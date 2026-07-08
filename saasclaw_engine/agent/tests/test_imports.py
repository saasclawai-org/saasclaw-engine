"""Import validation tests for all agent modules.

Catches missing imports and NameErrors by importing each module individually.
The agent modules were extracted from a monolithic runner.py into separate
files (activity.py, llm_backends.py, prompt_utils.py, context_scan.py,
tool_subtasks.py, tools.py) — this test ensures each split module imports
cleanly and exposes its expected public functions.
"""

import importlib

import pytest


AGENT_MODULES = [
    "saasclaw_engine.agent.activity",
    "saasclaw_engine.agent.context_scan",
    "saasclaw_engine.agent.llm_backends",
    "saasclaw_engine.agent.prompt_utils",
    "saasclaw_engine.agent.tool_subtasks",
    "saasclaw_engine.agent.tools",
    "saasclaw_engine.agent.runner",
]


@pytest.mark.parametrize("module_name", AGENT_MODULES)
def test_module_imports_cleanly(module_name):
    """Each agent module should import without ImportError or NameError."""
    mod = importlib.import_module(module_name)
    assert mod is not None


@pytest.mark.parametrize("module_name", AGENT_MODULES)
def test_module_has_logger(module_name):
    """Each agent module (except activity.py which is pure threading) should have a logger."""
    mod = importlib.import_module(module_name)
    if module_name == "saasclaw_engine.agent.activity":
        # activity.py is a pure threading module, no logger needed
        return
    assert hasattr(mod, "logger"), f"{module_name} missing module-level logger"


class TestActivityModule:
    """Verify activity.py exposes the thread-safe activity tracking API."""

    def test_get_agent_activity_exists(self):
        from saasclaw_engine.agent import activity
        assert hasattr(activity, "get_agent_activity")
        assert callable(activity.get_agent_activity)

    def test_set_agent_activity_exists(self):
        from saasclaw_engine.agent import activity
        assert hasattr(activity, "set_agent_activity")
        assert callable(activity.set_agent_activity)

    def test_clear_agent_activity_exists(self):
        from saasclaw_engine.agent import activity
        assert hasattr(activity, "clear_agent_activity")
        assert callable(activity.clear_agent_activity)

    def test_get_returns_dict(self):
        from saasclaw_engine.agent.activity import get_agent_activity
        result = get_agent_activity("nonexistent-session")
        assert isinstance(result, dict)
        assert "text" in result
        assert "tools_run" in result

    def test_set_then_get(self):
        from saasclaw_engine.agent.activity import set_agent_activity, get_agent_activity
        set_agent_activity("test-session-1", "working", tools_run=3)
        result = get_agent_activity("test-session-1")
        assert result["text"] == "working"
        assert result["tools_run"] == 3

    def test_clear_removes(self):
        from saasclaw_engine.agent.activity import set_agent_activity, clear_agent_activity, get_agent_activity
        set_agent_activity("test-session-2", "temp")
        clear_agent_activity("test-session-2")
        result = get_agent_activity("test-session-2")
        assert result["text"] == ""
        assert result["tools_run"] == 0


class TestLLMBackendsModule:
    """Verify llm_backends.py exposes the LLM provider API."""

    def test_llm_pricing_exists(self):
        from saasclaw_engine.agent import llm_backends
        assert hasattr(llm_backends, "LLM_PRICING")
        assert isinstance(llm_backends.LLM_PRICING, dict)

    def test_available_models_exists(self):
        from saasclaw_engine.agent import llm_backends
        assert hasattr(llm_backends, "AVAILABLE_MODELS")

    def test_estimate_cost_exists(self):
        from saasclaw_engine.agent import llm_backends
        assert callable(llm_backends._estimate_cost)

    def test_provider_config_exists(self):
        from saasclaw_engine.agent import llm_backends
        assert callable(llm_backends._provider_config)

    def test_call_openai_exists(self):
        from saasclaw_engine.agent import llm_backends
        assert callable(llm_backends._call_openai)

    def test_call_anthropic_exists(self):
        from saasclaw_engine.agent import llm_backends
        assert callable(llm_backends._call_anthropic)

    def test_call_llm_exists(self):
        from saasclaw_engine.agent import llm_backends
        assert callable(llm_backends._call_llm)

    def test_call_llm_stream_exists(self):
        from saasclaw_engine.agent import llm_backends
        assert callable(llm_backends._call_llm_stream)

    def test_max_tool_rounds_constant(self):
        from saasclaw_engine.agent import llm_backends
        assert hasattr(llm_backends, "MAX_TOOL_ROUNDS")

    def test_max_total_tool_calls_constant(self):
        from saasclaw_engine.agent import llm_backends
        assert hasattr(llm_backends, "MAX_TOTAL_TOOL_CALLS")

    def test_max_tool_cost_per_turn_constant(self):
        from saasclaw_engine.agent import llm_backends
        assert hasattr(llm_backends, "MAX_TOOL_COST_PER_TURN")

    def test_efficiency_warning_threshold_constant(self):
        from saasclaw_engine.agent import llm_backends
        assert hasattr(llm_backends, "EFFICIENCY_WARNING_THRESHOLD")


class TestPromptUtilsModule:
    """Verify prompt_utils.py exposes the prompt building API."""

    def test_system_prompt_exists(self):
        from saasclaw_engine.agent import prompt_utils
        assert callable(prompt_utils._system_prompt)

    def test_compact_conversation_exists(self):
        from saasclaw_engine.agent import prompt_utils
        assert callable(prompt_utils._compact_conversation)

    def test_is_permission_question_exists(self):
        from saasclaw_engine.agent import prompt_utils
        assert callable(prompt_utils._is_permission_question)

    def test_strip_permission_question_exists(self):
        from saasclaw_engine.agent import prompt_utils
        assert callable(prompt_utils._strip_permission_question)


class TestContextScanModule:
    """Verify context_scan.py exposes the codebase scanning API."""

    def test_scan_project_files_exists(self):
        from saasclaw_engine.agent import context_scan
        assert callable(context_scan._scan_project_files)

    def test_patch_context_on_tool_exists(self):
        from saasclaw_engine.agent import context_scan
        assert callable(context_scan._patch_context_on_tool)

    def test_do_patch_context_exists(self):
        from saasclaw_engine.agent import context_scan
        assert callable(context_scan._do_patch_context)

    def test_scan_codebase_context_exists(self):
        from saasclaw_engine.agent import context_scan
        assert callable(context_scan._scan_codebase_context)


class TestToolSubtasksModule:
    """Verify tool_subtasks.py exposes the subtask management API."""

    def test_background_command_exists(self):
        from saasclaw_engine.agent import tool_subtasks
        assert callable(tool_subtasks.background_command)

    def test_poll_command_exists(self):
        from saasclaw_engine.agent import tool_subtasks
        assert callable(tool_subtasks.poll_command)

    def test_spawn_subtask_exists(self):
        from saasclaw_engine.agent import tool_subtasks
        assert callable(tool_subtasks.spawn_subtask)

    def test_check_subtask_exists(self):
        from saasclaw_engine.agent import tool_subtasks
        assert callable(tool_subtasks.check_subtask)


class TestToolsModule:
    """Verify tools.py exposes the agent tool API."""

    def test_execute_tool_exists(self):
        from saasclaw_engine.agent import tools
        assert callable(tools.execute_tool)

    def test_read_file_exists(self):
        from saasclaw_engine.agent import tools
        assert callable(tools.read_file)

    def test_write_file_exists(self):
        from saasclaw_engine.agent import tools
        assert callable(tools.write_file)

    def test_replace_in_file_exists(self):
        from saasclaw_engine.agent import tools
        assert callable(tools.replace_in_file)

    def test_list_files_exists(self):
        from saasclaw_engine.agent import tools
        assert callable(tools.list_files)

    def test_git_status_exists(self):
        from saasclaw_engine.agent import tools
        assert callable(tools.git_status)

    def test_git_diff_exists(self):
        from saasclaw_engine.agent import tools
        assert callable(tools.git_diff)

    def test_git_commit_exists(self):
        from saasclaw_engine.agent import tools
        assert callable(tools.git_commit)

    def test_git_log_exists(self):
        from saasclaw_engine.agent import tools
        assert callable(tools.git_log)

    def test_run_command_exists(self):
        from saasclaw_engine.agent import tools
        assert callable(tools.run_command)

    def test_web_fetch_exists(self):
        from saasclaw_engine.agent import tools
        assert callable(tools.web_fetch)

    def test_web_search_exists(self):
        from saasclaw_engine.agent import tools
        assert callable(tools.web_search)

    def test_get_env_vars_exists(self):
        from saasclaw_engine.agent import tools
        assert callable(tools.get_env_vars)

    def test_set_env_var_exists(self):
        from saasclaw_engine.agent import tools
        assert callable(tools.set_env_var)

    def test_update_todos_exists(self):
        from saasclaw_engine.agent import tools
        assert callable(tools.update_todos)

    def test_max_output_constant(self):
        from saasclaw_engine.agent import tools
        assert hasattr(tools, "MAX_OUTPUT")
        assert tools.MAX_OUTPUT > 0


class TestRunnerModule:
    """Verify runner.py exposes the main agent entry point."""

    def test_run_agent_exists(self):
        from saasclaw_engine.agent import runner
        assert hasattr(runner, "run_agent")
        assert callable(runner.run_agent)

    def test_runner_imports_activity(self):
        """runner.py should import activity tracking functions."""
        from saasclaw_engine.agent import runner
        assert hasattr(runner, "get_agent_activity")
        assert hasattr(runner, "set_agent_activity")
        assert hasattr(runner, "clear_agent_activity")

    def test_runner_imports_llm_backends(self):
        """runner.py should import LLM backend functions."""
        from saasclaw_engine.agent import runner
        assert hasattr(runner, "_call_llm")
        assert hasattr(runner, "_call_llm_stream")
        assert hasattr(runner, "LLM_PRICING")

    def test_runner_imports_prompt_utils(self):
        """runner.py should import prompt building functions."""
        from saasclaw_engine.agent import runner
        assert hasattr(runner, "_system_prompt")
        assert hasattr(runner, "_compact_conversation")

    def test_runner_imports_context_scan(self):
        """runner.py should import context scanning functions."""
        from saasclaw_engine.agent import runner
        assert hasattr(runner, "_scan_project_files")
        assert hasattr(runner, "_patch_context_on_tool")

    def test_runner_imports_tools(self):
        """runner.py should import the tools module."""
        from saasclaw_engine.agent import runner
        assert hasattr(runner, "agent_tools")
