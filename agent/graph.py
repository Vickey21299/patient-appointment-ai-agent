"""LangGraph: decide-next-action (Gemini) <-> execute-tools loop.

Every node, router and LLM call has a verb-first, stable name, so the Langfuse trace tree and
Agent Graph read as actions rather than framework class names (Langfuse best practices).
"""
from __future__ import annotations

from typing import Callable, Literal

from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.runnables import RunnableLambda
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode


def build_graph(llm, tools: list, system_prompt: Callable[[], str]):
    model = llm.bind_tools(tools)

    def plan_next_step(state: MessagesState) -> dict:
        response = model.invoke([SystemMessage(system_prompt())] + state["messages"],
                                config={"run_name": "decide-next-action"})
        return {"messages": [response]}

    def route_after_decision(state: MessagesState) -> Literal["execute-tools", "__end__"]:
        last = state["messages"][-1]
        return "execute-tools" if isinstance(last, AIMessage) and last.tool_calls else END

    g = StateGraph(MessagesState)
    g.add_node("plan-next-step", plan_next_step)
    g.add_node("execute-tools", ToolNode(tools))
    g.add_edge(START, "plan-next-step")
    g.add_conditional_edges("plan-next-step", RunnableLambda(route_after_decision, name="route-next-step"),
                            {"execute-tools": "execute-tools", END: END})
    g.add_edge("execute-tools", "plan-next-step")
    return g.compile(name="run-scheduling-graph")
