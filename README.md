# Model-Agnostic Travel Agent — V1

This is the first working skeleton for the CMU capstone travel/workday coordination agent.

## What V1 demonstrates
- Model-provider abstraction
- Coordinator owns orchestration
- Tools are separated from reasoning
- Candidate-plan generation
- Critic/scoring logic
- Beam search
- No dependency on OpenAI, Anthropic, Gemini, or any specific model provider

The default provider is `MockProvider`, so the project runs immediately without API keys.

## Run
```bash
python app.py
```

## Run tests
```bash
python -m unittest discover -s tests
```

## Next build steps
1. Add OpenAI provider adapter
2. Add Anthropic provider adapter
3. Add Google Calendar tool
4. Add real flight data tool
5. Expose selected capabilities through MCP
6. Persist users, trips, preferences, and decisions
7. Deploy
