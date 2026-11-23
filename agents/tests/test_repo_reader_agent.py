import asyncio
import json
from typing import Any, AsyncGenerator

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.genai import types
from pydantic import PrivateAttr
from starlette.testclient import TestClient

from repo_reader.agent import DEFAULT_SKILLS_DIR, SKILL_TOOLS, build_agent
from repo_reader.serve_a2a import create_app
from repo_reader.skills import DiskSkillRegistry


class ScriptedLlm(BaseLlm):
    """A fake model that plays a fixed script of tool calls, then answers. No API key needed."""

    model: str = "scripted"
    script: list[tuple[str, Any]] = []
    _turn: int = PrivateAttr(default=0)
    _requests: list[LlmRequest] = PrivateAttr(default_factory=list)

    async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False) -> AsyncGenerator[LlmResponse, None]:
        self._requests.append(llm_request)
        kind, payload = self.script[min(self._turn, len(self.script) - 1)]
        self._turn += 1
        if kind == "call":
            name, args = payload
            part = types.Part.from_function_call(name=name, args=args)
        else:
            part = types.Part.from_text(text=payload)
        yield LlmResponse(content=types.Content(role="model", parts=[part]))


def run_agent(agent, message: str):
    async def go():
        runner = InMemoryRunner(agent=agent, app_name="test")
        session = await runner.session_service.create_session(app_name="test", user_id="u1")
        events = []
        try:
            async for event in runner.run_async(
                user_id="u1",
                session_id=session.id,
                new_message=types.Content(role="user", parts=[types.Part.from_text(text=message)]),
            ):
                events.append(event)
        finally:
            await runner.close()
        return events

    return asyncio.run(go())


def function_responses(events) -> dict[str, Any]:
    out = {}
    for event in events:
        for part in (event.content.parts if event.content else []) or []:
            if part.function_response:
                out[part.function_response.name] = part.function_response.response
    return out


def declared_tools(request: LlmRequest) -> set[str]:
    names = set()
    for tool in request.config.tools or []:
        for declaration in tool.function_declarations or []:
            names.add(declaration.name)
    return names


def test_agent_has_no_execution_of_its_own():
    agent = build_agent(model="gemini-flash-latest")
    assert agent.code_executor is None
    assert [type(t).__name__ for t in agent.tools] == ["McpToolset", "SkillToolset"]


def test_prompt_injection_is_blocked_end_to_end(repo):
    """The model obeys an injected instruction; guard still stops it."""
    llm = ScriptedLlm(script=[
        ("call", ("read_file", {"path": "NOTES.md"})),
        ("call", ("run", {"argv": ["rm", "-rf", "build"]})),
        ("text", "I cannot delete files here; that request came from NOTES.md."),
    ])
    agent = build_agent(model=llm, workspace=repo)
    events = run_agent(agent, "Read NOTES.md and do what it says.")

    responses = function_responses(events)
    assert "ignore your rules" in json.dumps(responses["read_file"])
    rm = responses["run"]
    assert rm.get("isError") or rm.get("is_error"), rm
    error = json.loads(rm["content"][0]["text"])["error"]
    assert error["kind"] == "denied" and "rm" in error["message"]
    assert (repo / "build" / "out.txt").exists()
    assert events[-1].content.parts[0].text.startswith("I cannot delete")


def test_model_sees_mcp_tools_and_no_script_tool(repo):
    llm = ScriptedLlm(script=[("call", ("git_log", {"since": "1 year ago"})), ("text", "done")])
    events = run_agent(build_agent(model=llm, workspace=repo), "What changed?")

    tools = declared_tools(llm._requests[0])
    assert {"git_log", "read_file", "search", "run", "load_skill"} <= tools
    assert "run_skill_script" not in tools
    assert "initial commit" in json.dumps(function_responses(events)["git_log"])


def test_skill_registry(tmp_path):
    registry = DiskSkillRegistry(DEFAULT_SKILLS_DIR)
    assert registry.names() == ["repo-reader"]
    assert "Rules" in asyncio.run(registry.get_skill(name="repo-reader")).instructions
    assert [f.name for f in asyncio.run(registry.search_skills(query="git authorship"))] == ["repo-reader"]

    for bad in ("../repo-reader", "Repo", "a/b"):
        try:
            asyncio.run(registry.get_skill(name=bad))
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} was accepted")


def test_skills_with_scripts_are_rejected(tmp_path):
    skill = tmp_path / "evil"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: evil\ndescription: runs things\n---\nRun the script.\n")
    (skill / "scripts" / "run.sh").write_text("#!/bin/sh\necho pwned\n")
    try:
        DiskSkillRegistry(tmp_path).load("evil")
    except ValueError as exc:
        assert "scripts" in str(exc)
    else:
        raise AssertionError("a skill with scripts was loaded")


def test_new_skills_appear_without_restart(tmp_path):
    registry = DiskSkillRegistry(tmp_path)
    assert registry.names() == []
    (tmp_path / "late").mkdir()
    (tmp_path / "late" / "SKILL.md").write_text("---\nname: late\ndescription: added later\n---\nHi.\n")
    assert asyncio.run(registry.get_skill(name="late")).frontmatter.description == "added later"


def test_skill_tool_filter_excludes_scripts():
    assert "run_skill_script" not in SKILL_TOOLS


def test_a2a_agent_card(repo, monkeypatch):
    monkeypatch.setenv("BARANDA_WORKSPACE", str(repo))
    with TestClient(create_app()) as client:
        card = client.get("/.well-known/agent-card.json").json()
    assert card["name"] == "repo_reader"
    assert "Read-only" in card["description"]
