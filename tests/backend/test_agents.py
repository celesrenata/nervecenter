"""Tests for backend agents subsystem."""


class TestAgentRegistry:
    """Tests for AgentRegistry."""

    def test_registry_exists(self):
        """Test that AgentRegistry can be imported."""
        from backend.agents.registry import AgentRegistry

        assert AgentRegistry is not None

    def test_native_agent_count(self):
        """Test that all 16 native agent types are registered."""
        from backend.agents.registry import AgentRegistry

        registry = AgentRegistry()
        types = registry.get_native_types()
        assert len(types) == 16, f"Expected 16 native agents, got {len(types)}"
