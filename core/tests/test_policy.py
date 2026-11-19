import pytest

from baranda.guard import Denied, ErrorKind, Guard, PolicyError, ToolError

from .conftest import write


def denied(guard, argv):
    with pytest.raises(Denied) as info:
        guard.check(argv)
    return info.value


# --- allowlist and denylist ------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["rm", "-rf", "build"],
        ["curl", "https://example.com"],
        ["bash", "-c", "ls"],
        ["git", "push"],
        ["git", "reset", "--hard"],
        ["git", "clean", "-fdx"],
        ["git", "-c", "core.pager=sh", "log"],
        ["git", "-C", "/", "log"],
        ["git"],
        ["/usr/bin/git", "log"],
        ["tokei"],
    ],
)
def test_denied_commands(guard, argv):
    denied(guard, argv)


def test_rm_reason_names_the_base_layer(guard):
    assert "(base)" in denied(guard, ["rm", "x"]).message


@pytest.mark.parametrize(
    "argv",
    [
        ["rg", "--pre", "sh", "x"],
        ["rg", "--pre=sh", "x"],
        ["rg", "-uL", "x"],
        ["rg", "-z", "x"],
        ["git", "diff", "--output=/tmp/x"],
        ["git", "log", "-p", "--output", "x"],
        ["git", "diff", "--ext-diff"],
        ["git", "log", "--textconv"],
    ],
)
def test_dangerous_flags_are_denied(guard, argv):
    denied(guard, argv)


def test_flags_after_double_dash_are_paths_not_flags(guard):
    # "--pre" after "--" is a path argument; it does not exist, so it is fine.
    guard.check(["rg", "x", "--", "--pre"])


@pytest.mark.parametrize(
    "argv",
    [
        ["rg", "root", "/etc"],
        ["rg", "x", "../"],
        ["git", "diff", "--no-index", "/etc/passwd", "README.md"],
        ["jq", ".", "/etc/os-release"],
        ["rg", "--ignore-file=/etc/passwd", "x"],
    ],
)
def test_paths_outside_workspace_are_denied(guard, argv):
    denied(guard, argv)


def test_denied_files_cannot_be_passed_as_arguments(guard, repo):
    write(repo / ".env", "TOKEN=abc")
    assert "pattern" in denied(guard, ["rg", "TOKEN", ".env"]).message


def test_allowed_git_gets_forced_arguments_and_env(guard, repo):
    prepared = guard.check(["git", "log", "--oneline"])
    argv = list(prepared.argv)
    assert argv[0] == "git"
    assert argv[argv.index("log") + 1 : argv.index("log") + 3] == ["--no-ext-diff", "--no-textconv"]
    assert argv[-1] == "--oneline"
    assert f"safe.directory={repo.resolve()}" in argv
    assert "core.fsmonitor=false" in argv
    assert argv.index("--no-pager") < argv.index("log")
    assert prepared.env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert prepared.env["HOME"] == "/nonexistent"
    assert prepared.cwd == repo.resolve()


def test_invalid_argv_is_invalid_input(guard):
    for argv in ([], "git log", ["git", 1], ["rg", "x" * 5000]):
        with pytest.raises(ToolError) as info:
            guard.check(argv)
        assert info.value.kind is ErrorKind.INVALID_INPUT


# --- layers ----------------------------------------------------------------------------------


def make_guard(repo, policy_dir, user_yaml):
    user = write(policy_dir / "user.yaml", user_yaml)
    return Guard(repo, [policy_dir / "connector.yaml", user])


def test_user_layer_can_restrict(repo, policy_dir):
    guard = make_guard(
        repo,
        policy_dir,
        "mode: restrict\ndeny_commands: [jq]\ncommands:\n  git:\n    subcommands: [log]\n    deny_flags: [--patch, -p]\n",
    )
    guard.check(["git", "log"])
    denied(guard, ["git", "show"])
    denied(guard, ["git", "log", "-p"])
    assert "user.yaml" in denied(guard, ["jq", "."]).message


def test_user_layer_cannot_widen(repo, policy_dir):
    guard = make_guard(repo, policy_dir, "mode: restrict\ncommands:\n  git:\n    subcommands: [log, push]\n  curl: {}\n")
    denied(guard, ["git", "push"])
    denied(guard, ["curl", "x"])


def test_only_commands(repo, policy_dir):
    guard = make_guard(repo, policy_dir, "mode: restrict\nonly_commands: [git]\n")
    guard.check(["git", "status"])
    denied(guard, ["rg", "x"])


@pytest.mark.parametrize(
    "user_yaml",
    [
        "mode: restrict\ncommands:\n  git:\n    env: {GIT_PAGER: sh}\n",
        "mode: restrict\ncommands:\n  git:\n    prefix_args: [-c, core.pager=sh]\n",
        "mode: restrict\ncommands:\n  git:\n    ok_exit_codes: [0, 1]\n",
        "mode: restrict\ntools:\n  read_file:\n    enabled: true\n",
        "mode: restrict\ncommands:\n  git:\n    deny_flag: [--patch]\n",
        "mode: grantt\n",
        "- not a mapping\n",
        "mode: restrict\nlimits:\n  timeout_seconds: -1\n",
    ],
)
def test_layers_reject_keys_their_mode_does_not_allow(repo, policy_dir, user_yaml):
    with pytest.raises(PolicyError):
        make_guard(repo, policy_dir, user_yaml)


def test_limits_take_the_minimum(repo, policy_dir):
    guard = make_guard(repo, policy_dir, "mode: restrict\nlimits:\n  timeout_seconds: 5\n  max_output_bytes: 99999999\n")
    assert guard.policy.limits.timeout_seconds == 5
    assert guard.policy.limits.max_output_bytes == 1048576


def test_two_grants_for_the_same_command_is_an_error(repo, policy_dir):
    other = write(policy_dir / "other.yaml", "mode: grant\ncommands:\n  git: {}\n")
    with pytest.raises(PolicyError):
        Guard(repo, [policy_dir / "connector.yaml", other])


def test_policy_hash_changes_with_content(repo, policy_dir):
    first = make_guard(repo, policy_dir, "mode: restrict\n").policy.hash
    second = make_guard(repo, policy_dir, "mode: restrict\ndeny_commands: [jq]\n").policy.hash
    assert first != second


# --- hot reload and fail closed --------------------------------------------------------------


def test_policy_reloads_when_a_file_changes(repo, policy_dir):
    guard = make_guard(repo, policy_dir, "mode: restrict\n")
    guard.check(["jq", "."])
    write(policy_dir / "user.yaml", "mode: restrict\ndeny_commands: [jq]\n# changed\n")
    denied(guard, ["jq", "."])


def test_broken_policy_after_start_denies_everything(repo, policy_dir):
    guard = make_guard(repo, policy_dir, "mode: restrict\n")
    guard.check(["git", "log"])
    write(policy_dir / "user.yaml", "mode: restrict\ncommands: [oops\n")
    assert "could not be loaded" in denied(guard, ["git", "log"]).message
    write(policy_dir / "user.yaml", "mode: restrict\n# fixed\n")
    guard.check(["git", "log"])


def test_invalid_policy_refuses_to_start(repo, policy_dir):
    with pytest.raises(PolicyError):
        make_guard(repo, policy_dir, "mode: restrict\nunknown_key: 1\n")
