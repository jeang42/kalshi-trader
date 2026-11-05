"""Tests for the user-profile system (multi-user Option A)."""

import importlib
from pathlib import Path

import pytest

from pm.core import profiles


@pytest.fixture
def profiles_root(tmp_path, monkeypatch):
    """Redirect PROFILES_ROOT to a tmp directory for each test."""
    monkeypatch.setattr(profiles, "PROFILES_ROOT", tmp_path / "data")
    monkeypatch.setattr(profiles, "ACTIVE_PROFILE_FILE",
                        tmp_path / "data" / ".active_profile")
    return tmp_path / "data"


class TestActiveProfileEnvOverride:
    def test_env_overrides_file(self, profiles_root, tmp_path, monkeypatch):
        profiles.initialize_profiles(project_root=tmp_path)
        profiles.set_active_profile("jeff")          # file says jeff
        assert profiles.active_profile() == "jeff"
        # Env override wins when it names an existing profile.
        monkeypatch.setenv(profiles.ACTIVE_PROFILE_ENV, "agentic")
        assert profiles.active_profile() == "agentic"

    def test_env_ignored_when_profile_missing(self, profiles_root, tmp_path,
                                              monkeypatch):
        profiles.initialize_profiles(project_root=tmp_path)
        profiles.set_active_profile("jeff")
        monkeypatch.setenv(profiles.ACTIVE_PROFILE_ENV, "does_not_exist")
        assert profiles.active_profile() == "jeff"   # falls back to the file


class TestInitializeProfiles:
    def test_creates_data_directory_and_defaults(self, profiles_root, tmp_path):
        profiles.initialize_profiles(project_root=tmp_path)
        assert profiles_root.exists()
        for name in profiles.DEFAULT_PROFILES:
            assert (profiles_root / name).is_dir()
        assert profiles.shared_dir().is_dir()

    def test_writes_default_active_profile(self, profiles_root, tmp_path):
        profiles.initialize_profiles(project_root=tmp_path)
        assert profiles.ACTIVE_PROFILE_FILE.exists()
        assert profiles.ACTIVE_PROFILE_FILE.read_text().strip() == "jeff"

    def test_idempotent(self, profiles_root, tmp_path):
        profiles.initialize_profiles(project_root=tmp_path)
        # Drop a file in jeff/ then re-initialize — must not be deleted
        (profiles_root / "jeff" / "test.txt").write_text("hello")
        profiles.initialize_profiles(project_root=tmp_path)
        assert (profiles_root / "jeff" / "test.txt").exists()

    def test_does_not_overwrite_active_profile_choice(self, profiles_root, tmp_path):
        profiles.initialize_profiles(project_root=tmp_path)
        profiles.set_active_profile("dex")
        # Re-initialize — active should stay 'dex', not revert to 'jeff'
        profiles.initialize_profiles(project_root=tmp_path)
        assert profiles.active_profile() == "dex"


class TestLegacyMigration:
    def test_migrates_root_level_files_to_jeff(self, profiles_root, tmp_path):
        # Simulate a pre-profile install
        (tmp_path / "portfolio.json").write_text('{"version": 2, "transactions": []}')
        (tmp_path / "forecasts.json").write_text('{"version": 1, "forecasts": []}')
        (tmp_path / "alerts.json").write_text('{"version": 1, "alerts": []}')
        (tmp_path / "settings.json").write_text('{"bankroll": 5000.0}')

        profiles.initialize_profiles(project_root=tmp_path)

        # Files should now live under data/jeff/, not the project root
        for kind in profiles.PROFILE_FILE_KINDS:
            assert (profiles_root / "jeff" / f"{kind}.json").exists()
            assert not (tmp_path / f"{kind}.json").exists()

    def test_preserves_existing_jeff_files(self, profiles_root, tmp_path):
        """If data/jeff/ already has a file, the migration must NOT overwrite it."""
        (profiles_root / "jeff").mkdir(parents=True)
        (profiles_root / "jeff" / "portfolio.json").write_text('{"existing": true}')
        # Legacy root-level file should remain at root since target exists
        (tmp_path / "portfolio.json").write_text('{"legacy": true}')

        profiles.initialize_profiles(project_root=tmp_path)

        assert (profiles_root / "jeff" / "portfolio.json").read_text() == '{"existing": true}'
        # Legacy stays because target was already there
        assert (tmp_path / "portfolio.json").exists()

    def test_no_legacy_files_no_migration(self, profiles_root, tmp_path):
        """No-op when there's nothing to migrate."""
        profiles.initialize_profiles(project_root=tmp_path)
        # data/jeff/ exists but is empty
        for kind in profiles.PROFILE_FILE_KINDS:
            assert not (profiles_root / "jeff" / f"{kind}.json").exists()


class TestListProfiles:
    def test_returns_defaults_when_empty(self, profiles_root):
        # When data/ doesn't exist yet, returns the declared default order
        # (primary profile first). Once directories exist, results are sorted.
        assert profiles.list_profiles() == list(profiles.DEFAULT_PROFILES)

    def test_lists_actual_directories(self, profiles_root, tmp_path):
        profiles.initialize_profiles(project_root=tmp_path)
        profiles.create_profile("custom")
        names = profiles.list_profiles()
        assert "custom" in names
        assert "jeff" in names

    def test_excludes_shared_and_dotfiles(self, profiles_root, tmp_path):
        profiles.initialize_profiles(project_root=tmp_path)
        names = profiles.list_profiles()
        assert "shared" not in names


class TestActiveProfile:
    def test_default_when_no_file(self, profiles_root):
        # No .active_profile file → returns DEFAULT_ACTIVE_PROFILE
        # (assuming the default profile exists, which it doesn't yet)
        # In this empty state, returns DEFAULT_ACTIVE_PROFILE as the fallback
        assert profiles.active_profile() == "jeff"

    def test_reads_from_file(self, profiles_root, tmp_path):
        profiles.initialize_profiles(project_root=tmp_path)
        profiles.set_active_profile("dex")
        assert profiles.active_profile() == "dex"

    def test_falls_back_when_active_points_to_missing(self, profiles_root, tmp_path):
        profiles.initialize_profiles(project_root=tmp_path)
        # Manually write a bogus active profile that doesn't have a directory
        profiles.ACTIVE_PROFILE_FILE.write_text("nonexistent")
        # Falls back to a real profile
        assert profiles.active_profile() in profiles.DEFAULT_PROFILES


class TestCreateProfile:
    def test_creates(self, profiles_root, tmp_path):
        profiles.initialize_profiles(project_root=tmp_path)
        assert profiles.create_profile("alice") is True
        assert (profiles_root / "alice").is_dir()

    def test_returns_false_when_already_exists(self, profiles_root, tmp_path):
        profiles.initialize_profiles(project_root=tmp_path)
        assert profiles.create_profile("jeff") is False

    def test_rejects_invalid_names(self, profiles_root, tmp_path):
        profiles.initialize_profiles(project_root=tmp_path)
        with pytest.raises(ValueError):
            profiles.create_profile("")
        with pytest.raises(ValueError):
            profiles.create_profile("../etc")
        with pytest.raises(ValueError):
            profiles.create_profile(".hidden")
        with pytest.raises(ValueError):
            profiles.create_profile("shared")


class TestResetProfile:
    def test_wipes_files(self, profiles_root, tmp_path):
        profiles.initialize_profiles(project_root=tmp_path)
        (profiles_root / "sandbox" / "portfolio.json").write_text("x")
        (profiles_root / "sandbox" / "alerts.json").write_text("y")
        n = profiles.reset_profile("sandbox")
        assert n == 2
        # Directory still exists; files are gone
        assert (profiles_root / "sandbox").is_dir()
        assert not (profiles_root / "sandbox" / "portfolio.json").exists()

    def test_nonexistent_profile_no_error(self, profiles_root):
        assert profiles.reset_profile("does-not-exist") == 0


class TestFilePath:
    def test_returns_per_profile_path(self, profiles_root, tmp_path):
        profiles.initialize_profiles(project_root=tmp_path)
        p = profiles.file_path("portfolio", "dex")
        assert p == profiles_root / "dex" / "portfolio.json"

    def test_uses_active_profile_when_none(self, profiles_root, tmp_path):
        profiles.initialize_profiles(project_root=tmp_path)
        profiles.set_active_profile("dex")
        p = profiles.file_path("portfolio")
        assert p.parent.name == "dex"

    def test_rejects_unknown_kind(self, profiles_root):
        with pytest.raises(ValueError):
            profiles.file_path("nonexistent", "jeff")


class TestLazyPathLookupIntegration:
    """Verify the data modules read paths lazily via env-var override AND
    via the profile system, in that priority order."""

    def test_portfolio_uses_active_profile_by_default(self, profiles_root, tmp_path, monkeypatch):
        # Make sure no env override is set
        monkeypatch.delenv("POLYMARKET_PORTFOLIO_FILE", raising=False)
        profiles.initialize_profiles(project_root=tmp_path)
        profiles.set_active_profile("dex")

        from pm.ledger import portfolio
        # Path should come from dex profile, not the legacy default
        path = portfolio._portfolio_path()
        assert path.parent.name == "dex"

    def test_env_var_overrides_profile(self, profiles_root, tmp_path, monkeypatch):
        profiles.initialize_profiles(project_root=tmp_path)
        profiles.set_active_profile("dex")
        override = tmp_path / "elsewhere.json"
        monkeypatch.setenv("POLYMARKET_PORTFOLIO_FILE", str(override))

        from pm.ledger import portfolio
        assert portfolio._portfolio_path() == override
