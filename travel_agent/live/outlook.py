"""Folder delta infrastructure behind MailSource; no travel or persistence logic."""
import json

from travel_agent.live.graph_client import (GraphFailure, GraphLimits, require, identifier,
    decode_json, route_url, validate_url, ORIGIN)
from travel_agent.live.graph_normalization import (LIGHT_SELECT, FULL_SELECT, instant,
    metadata, normalize, validate_attachments)
from travel_agent.live.providers import (MailSyncPage, ProviderResult, ProviderError,
    ProviderErrorCode as Code, RetainedMailIdentityLookup)
from travel_agent.live.time import utc


FOLDER_SELECT = "?$select=id,parentFolderId,isHidden,childFolderCount"


class OutlookMailSource:
    """One atomic candidate page; every provider traversal stays attempt-local."""
    def __init__(self, *, client, retained_identity: RetainedMailIdentityLookup, limits=None):
        if not callable(retained_identity):
            raise ValueError("Read-only retained identity lookup required")
        self.client, self._retained = client, retained_identity
        self.binding, self.limits = client.binding, limits or GraphLimits()
        self.account_id = self.binding.account_id

    def _context(self):
        return dict(format="graph-mail-cursor/v1", provider="OUTLOOK_MAIL",
                    account_id=self.account_id, api_origin=ORIGIN, api_version="v1.0",
                    mailbox_user_id=self.binding.mailbox_user_id, id_profile="ImmutableId",
                    normalization_profile="graph-mail-normalization-v1",
                    discovery_profile="graph-normal-folders/v1", mode="completed-folder-delta-vector")

    def _load_cursor(self, cursor):
        try:
            require(isinstance(cursor, str) and len(cursor.encode("utf-8")) <= self.limits.cursor_bytes)
            value = decode_json(cursor.encode("utf-8"), self.limits.json_depth)
            context = self._context()
            require(set(value) == set(context) | {"initial_lower_bound", "folders"})
            require(all(value[k] == v for k, v in context.items()))
            lower = instant(value["initial_lower_bound"])
            require(isinstance(value["folders"], list) and 0 < len(value["folders"]) <= self.limits.folders)
            folders, links = {}, {}
            for folder in value["folders"]:
                require(isinstance(folder, dict) and set(folder) == {"id", "parent_id", "delta_url"})
                fid, parent = identifier(folder["id"]), identifier(folder["parent_id"])
                require(fid not in folders and fid != parent)
                folders[fid] = parent
                links[fid] = validate_url(folder["delta_url"], self.binding,
                    ("mailFolders", fid, "messages", "delta"), self.limits.link_bytes)
            require(list(folders) == sorted(folders))
            roots = [fid for fid, parent in folders.items() if parent not in folders]
            require(len(roots) == 1)
            for fid in folders:
                seen = set()
                while fid in folders:
                    require(fid not in seen and len(seen) < self.limits.folder_depth)
                    seen.add(fid)
                    fid = folders[fid]
            return lower, folders, links
        except Exception:
            raise GraphFailure(Code.CURSOR_EXPIRED) from None

    def _pages(self, attempt, route, *, query="", url=None, delta=False, established=False):
        current = url if url is not None else route_url(self.binding, route, query)
        initial, visited, entries = current, set(), 0
        while True:
            require(current not in visited)
            visited.add(current)
            attempt.pages += 1
            require(attempt.pages <= self.limits.pages)
            result = attempt.read(route, url=current, delta=delta, established=established)
            require(isinstance(result.get("value"), list))
            entries += len(result["value"])
            if delta:
                attempt.entries += len(result["value"])
                require(attempt.entries <= self.limits.delta_entries)
            next_url, terminal = result.get("@odata.nextLink"), result.get("@odata.deltaLink")
            if delta:
                require((next_url is None) != (terminal is None))
            else:
                require(terminal is None)
            for link in (next_url, terminal):
                if link is not None:
                    validate_url(link, self.binding, route, self.limits.link_bytes)
            yield result["value"], terminal
            if next_url is None:
                if delta and entries and terminal == initial:
                    raise GraphFailure(Code.UNAVAILABLE, True)
                return
            current = next_url

    def _inventory(self, attempt):
        root = attempt.read(("mailFolders", "msgfolderroot"), query=FOLDER_SELECT)
        pending, folders, enumerated = [(root, None, 1)], {}, set()
        while pending:
            value, expected_parent, depth = pending.pop()
            require(isinstance(value, dict))
            fid, parent = identifier(value.get("id")), identifier(value.get("parentFolderId"))
            require(fid not in enumerated and fid != parent and depth <= self.limits.folder_depth)
            enumerated.add(fid)
            attempt.enumerated_folders += 1
            require(attempt.enumerated_folders <= self.limits.enumerated_folders)
            require(expected_parent is None or parent == expected_parent)
            require(type(value.get("isHidden")) is bool
                    and type(value.get("childFolderCount")) is int and value["childFolderCount"] >= 0)
            kind = value.get("@odata.type", "#microsoft.graph.mailFolder")
            require(kind in ("#microsoft.graph.mailFolder", "#microsoft.graph.mailSearchFolder"),
                    Code.UNSUPPORTED_CAPABILITY)
            if value["isHidden"] or kind == "#microsoft.graph.mailSearchFolder":
                require(expected_parent is not None, Code.UNSUPPORTED_CAPABILITY)
                continue
            folders[fid] = parent
            require(len(folders) <= self.limits.folders)
            # Enumerate even a reported zero: counts are hints and can race.
            children = []
            for items, _ in self._pages(attempt, ("mailFolders", fid, "childFolders"),
                                      query=FOLDER_SELECT + "&includeHiddenFolders=true"):
                children.extend(items)
                require(len(children) + attempt.enumerated_folders + len(pending) <= self.limits.enumerated_folders)
            if len(children) != value["childFolderCount"]:
                raise GraphFailure(Code.UNAVAILABLE, True)
            pending.extend((item, fid, depth + 1) for item in reversed(children))
        return dict(sorted(folders.items()))

    def _delta(self, attempt, folders, previous=None, *, quiet=False):
        hints, terminals = {}, {}
        for fid in sorted(folders):
            route = ("mailFolders", fid, "messages", "delta")
            for items, terminal in self._pages(attempt, route, query="?$select=" + LIGHT_SELECT,
                    url=previous[fid] if previous else None, delta=True, established=previous is not None):
                if quiet and items:
                    raise GraphFailure(Code.UNAVAILABLE, True)
                for item in items:
                    require(isinstance(item, dict))
                    mid = identifier(item.get("id"))
                    removed = "@removed" in item
                    if removed:
                        require(item["@removed"] == {"reason": "deleted"})
                    # Hints are never used as a body, identity association or
                    # last-writer-wins snapshot. Every ID gets a current read.
                    hints[mid] = hints.get(mid, False) or removed
                    require(len(hints) <= self.limits.candidate_ids)
                if terminal is not None:
                    terminals[fid] = terminal
        require(set(terminals) == set(folders))
        return hints, terminals

    def sync(self, *, since, cursor, page_token=None):
        try:
            require(page_token is None, Code.UNSUPPORTED_CAPABILITY)
            lower, previous_folders, previous_links = utc(since), None, None
            if cursor is not None:
                lower, previous_folders, previous_links = self._load_cursor(cursor)
            attempt = self.client.begin(self.limits)
            try:
                folders = self._inventory(attempt)
            except GraphFailure as failure:
                if failure.error.code == Code.NOT_FOUND:
                    raise GraphFailure(Code.CURSOR_EXPIRED if cursor is not None else Code.UNAVAILABLE,
                                       cursor is None) from None
                raise
            if previous_folders is not None and previous_folders != folders:
                raise GraphFailure(Code.CURSOR_EXPIRED)
            hints, staged = self._delta(attempt, folders, previous_links)
            messages, removals = [], []
            for mid, removed in sorted(hints.items()):
                attempt.check()
                retained = self._retained(provider="OUTLOOK_MAIL", account_id=self.account_id, message_id=mid)
                require(type(retained) is bool)
                route = ("messages", mid)
                try:
                    current = attempt.read(route, query="?$select=" + LIGHT_SELECT)
                except GraphFailure as failure:
                    if failure.error.code == Code.NOT_FOUND and removed and cursor is not None:
                        if retained:
                            removals.append(mid)
                        continue
                    raise
                state = metadata(current, mid)
                parent, received, draft, _ = state
                if parent not in folders or draft:
                    if retained:
                        removals.append(mid)
                    continue
                if received < lower and not retained:
                    continue
                full = attempt.read(route, query="?$select=" + FULL_SELECT, body=True)
                if metadata(full, mid) != state:
                    raise GraphFailure(Code.UNAVAILABLE, True)
                normalized = normalize(full, binding=self.binding, expected_id=mid, limits=self.limits)
                attachments = []
                for items, _ in self._pages(attempt, (*route, "attachments"),
                                           query="?$select=id,contentType,isInline,size"):
                    attachments.extend(items)
                    require(len(attachments) <= self.limits.attachments)
                validate_attachments(attachments, has_attachments=full["hasAttachments"], limits=self.limits)
                messages.append(normalized)
            require(len(messages) + len(removals) <= self.limits.emitted_items)
            _, terminals = self._delta(attempt, folders, staged, quiet=True)
            try:
                verified_folders = self._inventory(attempt)
            except GraphFailure as failure:
                if failure.error.code == Code.NOT_FOUND:
                    raise GraphFailure(Code.UNAVAILABLE, True) from None
                raise
            if verified_folders != folders:
                raise GraphFailure(Code.UNAVAILABLE, True)
            complete = self._context() | dict(initial_lower_bound=lower.isoformat(), folders=[
                dict(id=fid, parent_id=parent, delta_url=terminals[fid]) for fid, parent in folders.items()])
            encoded = json.dumps(complete, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            require(len(encoded.encode("utf-8")) <= self.limits.cursor_bytes)
            attempt.check()
            return ProviderResult(value=MailSyncPage(tuple(messages), tuple(removals), None, encoded))
        except GraphFailure as failure:
            return ProviderResult(error=failure.error)
        except Exception:
            return ProviderResult(error=ProviderError(Code.INVALID_RESPONSE, False))
