"""workspace_git 凭证处理测试。

回归背景：origin 曾存 `https://user:token@host/...`，`git remote -v` 与
.git/config 会直接暴露明文 token。改为 origin 只存干净 URL，凭证写入
.git/cili-credentials 并由 credential.helper 提供。
"""

import subprocess

import pytest

from core.workspace_git import (
    _CREDENTIALS_FILENAME,
    _clean_remote_url,
    _credentials_path,
    _find_git,
    _git_cmd,
    _git_net_cmd,
    _write_credentials_file,
)

TOKEN = "ghp_AbCdEf1234567890"
REMOTE = "https://github.com/owner/repo.git"


class TestCleanRemoteUrl:
    def test_strips_userinfo(self):
        assert _clean_remote_url(f"https://user:{TOKEN}@github.com/owner/repo.git") == REMOTE

    def test_strips_username_only(self):
        assert _clean_remote_url("https://user@github.com/owner/repo.git") == REMOTE

    def test_keeps_port_and_path(self):
        assert _clean_remote_url(f"https://u:{TOKEN}@host:8443/a/b.git") == "https://host:8443/a/b.git"

    def test_clean_url_unchanged(self):
        assert _clean_remote_url(REMOTE) == REMOTE

    def test_ssh_unchanged(self):
        assert _clean_remote_url("git@github.com:owner/repo.git") == "git@github.com:owner/repo.git"
        assert _clean_remote_url("ssh://git@host/repo.git") == "ssh://git@host/repo.git"


class TestWriteCredentialsFile:
    def _path(self, tmp_path):
        return _credentials_path(tmp_path)

    def test_writes_credential_store_line(self, tmp_path):
        (tmp_path / ".git").mkdir()
        _write_credentials_file(tmp_path, REMOTE, "user", TOKEN)
        content = self._path(tmp_path).read_text(encoding="utf-8")
        assert content == f"https://user:{TOKEN}@github.com\n"

    def test_percent_encodes_special_chars(self, tmp_path):
        (tmp_path / ".git").mkdir()
        _write_credentials_file(tmp_path, REMOTE, "u@ser", "to:ken/with@chars")
        content = self._path(tmp_path).read_text(encoding="utf-8")
        assert "u%40ser" in content
        assert "to%3Aken%2Fwith%40chars" in content

    def test_removes_file_when_no_token(self, tmp_path):
        (tmp_path / ".git").mkdir()
        _write_credentials_file(tmp_path, REMOTE, "user", TOKEN)
        assert self._path(tmp_path).exists()

        _write_credentials_file(tmp_path, REMOTE, "", "")
        assert not self._path(tmp_path).exists()

    def test_removes_file_for_ssh_remote(self, tmp_path):
        (tmp_path / ".git").mkdir()
        _write_credentials_file(tmp_path, REMOTE, "user", TOKEN)
        _write_credentials_file(tmp_path, "git@github.com:owner/repo.git", "user", TOKEN)
        assert not self._path(tmp_path).exists()


@pytest.fixture
def git_repo(tmp_path):
    """一个真实的空 git 仓库（git 不可用时跳过）。"""
    if not _find_git():
        pytest.skip("git not available")
    repo = tmp_path / "repo"
    repo.mkdir()
    if _git_cmd(repo, ["init", "-q"]).returncode != 0:
        pytest.skip("git init failed")
    return repo


class TestRemoteUrlHasNoToken:
    """端到端：设置 origin 后，git 配置里不得出现明文 token。"""

    def test_remote_url_and_config_are_clean(self, git_repo):
        _write_credentials_file(git_repo, REMOTE, "user", TOKEN)
        clean = _clean_remote_url(f"https://user:{TOKEN}@github.com/owner/repo.git")
        assert _git_cmd(git_repo, ["remote", "add", "origin", clean]).returncode == 0

        shown = _git_cmd(git_repo, ["remote", "get-url", "origin"]).stdout
        assert shown.strip() == REMOTE
        assert TOKEN not in shown

        # 完整配置导出中也不得出现 token
        config_dump = _git_cmd(git_repo, ["config", "--list"]).stdout
        assert TOKEN not in config_dump

        # 凭证在 .git/ 下的独立文件中（不会被提交、不会出现在 remote -v）
        assert _credentials_path(git_repo).is_file()

    def test_net_cmd_injects_credential_helper(self, git_repo):
        _write_credentials_file(git_repo, REMOTE, "user", TOKEN)
        # -c 覆盖会反映在 config 查询结果里，据此确认 helper 已挂上
        out = _git_net_cmd(git_repo, ["config", "--get", "credential.helper"]).stdout
        assert f"store --file=.git/{_CREDENTIALS_FILENAME}" in out

    def test_net_cmd_without_credentials_has_no_helper(self, git_repo):
        out = _git_net_cmd(git_repo, ["config", "--get", "credential.helper"]).stdout
        assert "cili-credentials" not in out

    def test_credential_fill_reads_workspace_token(self, git_repo):
        """端到端：git 取到的必须是本工作区 token，而非用户全局凭据。

        只写 `-c credential.helper=store --file=...` 是追加到 helper 链，
        用户全局 helper 会先命中（实测取到全局 token）—— 必须先用空值清空。
        """
        import os as _os

        _write_credentials_file(git_repo, REMOTE, "user", TOKEN)
        env = {**_os.environ, "GIT_TERMINAL_PROMPT": "0"}
        proc = subprocess.run(
            [
                _find_git(),
                "-c", "credential.helper=",
                "-c", f"credential.helper=store --file=.git/{_CREDENTIALS_FILENAME}",
                "credential", "fill",
            ],
            cwd=str(git_repo),
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        assert "username=user" in proc.stdout
        assert f"password={TOKEN}" in proc.stdout
