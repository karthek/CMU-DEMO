from abc import ABC, abstractmethod

class ModelProvider(ABC):
    @abstractmethod
    def generate_candidate_plans(self, context, count=4):
        raise NotImplementedError

    @abstractmethod
    def summarize_recommendation(self, context, finalists):
        raise NotImplementedError
