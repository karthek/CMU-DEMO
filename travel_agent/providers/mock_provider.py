from travel_agent.planning.baseline import generate_baseline_candidates, summarize_recommendation
from travel_agent.providers.base import ModelProvider

class MockProvider(ModelProvider):
    def generate_candidate_plans(self, context, count=4):
        return generate_baseline_candidates(context, count)

    def summarize_recommendation(self, context, finalists):
        return summarize_recommendation(context, finalists)
