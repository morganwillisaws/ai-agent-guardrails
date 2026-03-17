import os
from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient
from mcp.client.streamable_http import streamablehttp_client

gateway_url = os.environ["GATEWAY_URL"]
token = os.environ["ID_TOKEN"]

model = BedrockModel(model_id="us.anthropic.claude-sonnet-4-20250514-v1:0")

mcp = MCPClient(lambda: streamablehttp_client(gateway_url, headers={"Authorization": f"Bearer {token}"}))

with mcp:
    tools = mcp.list_tools_sync()
    print(f"Loaded {len(tools)} tools")
    agent = Agent(model=model, tools=tools)
    while True:
        prompt = input("\nYou: ")
        if prompt.lower() in ("quit", "exit"):
            break
        print("\nAgent:", agent(prompt))
