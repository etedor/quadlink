"""Tests for configuration loader."""

import tempfile
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.scanner import ScannerError

from quadlink.config.loader import ConfigLoader, ConfigNotFoundError


def write_yaml(path: Path, data: dict[str, Any]) -> None:
    """Write data to a YAML file."""
    yaml = YAML()
    with open(path, "w") as f:
        yaml.dump(data, f)


@pytest.fixture
def minimal_config_data():
    """Minimal valid configuration data."""
    return {
        "credentials": {"username": "test", "secret": "secret"},
        "priorities": {999: [{"urls": ["streamer1"]}]},
    }


@pytest.fixture
def temp_config_file(minimal_config_data):
    """Create a temporary config file."""
    yaml = YAML()
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(minimal_config_data, f)
        return Path(f.name)


@pytest.mark.asyncio
async def test_load_config_not_found():
    """Test loading config when no file exists."""
    loader = ConfigLoader()
    # override search paths to non-existent locations
    loader.SEARCH_PATHS = ["/tmp/nonexistent.yaml"]

    with pytest.raises(ConfigNotFoundError, match="No config file found"):
        await loader.load_or_cache()


@pytest.mark.asyncio
async def test_load_config_from_current_dir(minimal_config_data, tmp_path):
    """Test loading config from current directory."""
    # create config in a temp directory
    config_file = tmp_path / "config.yaml"
    write_yaml(config_file, minimal_config_data)

    loader = ConfigLoader()
    loader.SEARCH_PATHS = [str(config_file)]

    config = await loader.load_or_cache()
    assert config.credentials.username == "test"
    assert 999 in config.priorities


@pytest.mark.asyncio
async def test_config_caching(minimal_config_data, tmp_path):
    """Test that config is cached after first load."""
    config_file = tmp_path / "config.yaml"
    write_yaml(config_file, minimal_config_data)

    loader = ConfigLoader()
    loader.SEARCH_PATHS = [str(config_file)]

    # first load - should succeed
    config1 = await loader.load_or_cache()
    assert config1.credentials.username == "test"

    # delete the file
    config_file.unlink()

    # second load - should use cache
    config2 = await loader.load_or_cache()
    assert config2.credentials.username == "test"
    assert config1 is config2  # same object


@pytest.mark.asyncio
async def test_config_cache_on_error(minimal_config_data, tmp_path):
    """Test fallback to cache on load error."""
    config_file = tmp_path / "config.yaml"
    write_yaml(config_file, minimal_config_data)

    loader = ConfigLoader()
    loader.SEARCH_PATHS = [str(config_file)]

    # first load - succeeds and caches
    config1 = await loader.load_or_cache()
    assert config1.credentials.username == "test"

    # corrupt the file
    with open(config_file, "w") as f:
        f.write("invalid: yaml: content: {")

    # second load - should use cache despite error
    config2 = await loader.load_or_cache()
    assert config2.credentials.username == "test"


@pytest.mark.asyncio
async def test_no_cache_on_first_error(tmp_path):
    """Test that first load error raises exception."""
    config_file = tmp_path / "config.yaml"
    with open(config_file, "w") as f:
        f.write("invalid: yaml: content: {")

    loader = ConfigLoader()
    loader.SEARCH_PATHS = [str(config_file)]

    # first load with invalid file
    with pytest.raises((ScannerError, ValidationError, ValueError)):
        await loader.load_or_cache()


@pytest.mark.asyncio
async def test_has_config_when_file_exists(minimal_config_data, tmp_path):
    """Test has_config when file exists."""
    config_file = tmp_path / "config.yaml"
    write_yaml(config_file, minimal_config_data)

    loader = ConfigLoader()
    loader.SEARCH_PATHS = [str(config_file)]

    assert await loader.has_config() is True


@pytest.mark.asyncio
async def test_has_config_when_cached(minimal_config_data, tmp_path):
    """Test has_config when config is cached."""
    config_file = tmp_path / "config.yaml"
    write_yaml(config_file, minimal_config_data)

    loader = ConfigLoader()
    loader.SEARCH_PATHS = [str(config_file)]

    # load and cache
    await loader.load_or_cache()

    # delete file
    config_file.unlink()

    # should still have config (from cache)
    assert await loader.has_config() is True


@pytest.mark.asyncio
async def test_has_config_when_none(tmp_path):
    """Test has_config when no config exists."""
    loader = ConfigLoader()
    loader.SEARCH_PATHS = [str(tmp_path / "nonexistent.yaml")]

    assert await loader.has_config() is False


@pytest.mark.asyncio
async def test_load_with_bonus_adjustment(tmp_path):
    """Test loading config with bonus auto-adjustment."""
    config_data = {
        "credentials": {"username": "test", "secret": "secret"},
        "priorities": {999: [{"urls": ["streamer1"]}]},
        "diversity_bonus": 25,
        "stability_bonus": 20,  # too low
    }
    config_file = tmp_path / "config.yaml"
    write_yaml(config_file, config_data)

    loader = ConfigLoader()
    loader.SEARCH_PATHS = [str(config_file)]

    config = await loader.load_or_cache()

    # should be auto-adjusted
    assert config.stability_bonus == 26
    assert config.diversity_bonus == 25


@pytest.mark.asyncio
async def test_load_from_first_available_path(tmp_path):
    """Test that loader uses first available config file."""
    # create two config files
    config1 = tmp_path / "config1.yaml"
    config2 = tmp_path / "config2.yaml"

    data1 = {
        "credentials": {"username": "user1", "secret": "secret"},
        "priorities": {999: [{"urls": ["streamer1"]}]},
    }

    data2 = {
        "credentials": {"username": "user2", "secret": "secret"},
        "priorities": {999: [{"urls": ["streamer1"]}]},
    }

    write_yaml(config1, data1)
    write_yaml(config2, data2)

    loader = ConfigLoader()
    loader.SEARCH_PATHS = [str(config1), str(config2)]

    config = await loader.load_or_cache()

    # should load from first path
    assert config.credentials.username == "user1"
