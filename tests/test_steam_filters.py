import pytest
from umeyc_steamback.steam import is_compatibility_tool


@pytest.mark.parametrize("name", ["Proton 9.0", "Proton - Experimental", "Proton Hotfix", "GE-Proton9-25", "Proton-GE", "Steam Linux Runtime 3.0 (sniper)", "Steam Linux Runtime - Soldier"])
def test_compatibility_tools_are_hidden(name):
    assert is_compatibility_tool(name)


@pytest.mark.parametrize("name", ["Proton Pulse", "明末：渊虚之羽", "SteamWorld Dig", "Portal 2"])
def test_real_games_are_preserved(name):
    assert not is_compatibility_tool(name)

