# Sample Demo Task

Ask the agent:

```text
Create a Python file named calc.py with add and multiply functions, then run a short command to verify both functions.
```

Suggested live API config:

```text
# config/llm.env
CODE_AGENT_BASE_URL=https://api.openai.com/v1
CODE_AGENT_MODEL=gpt-4o-mini
CODE_AGENT_API_KEY=your-api-key
```

Run:

```powershell
python run.py "Create a Python file named calc.py with add and multiply functions, then run a short command to verify both functions."
```
