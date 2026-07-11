"""Tests for deployment phase tracking in _set_deploy_phase."""
import pytest
from unittest.mock import patch, MagicMock

from saasclaw_engine.deployments.service import _set_deploy_phase


class TestSetDeployPhase:

    def test_sets_current_phase(self):
        deployment = MagicMock()
        deployment.metadata_json = {}
        _set_deploy_phase(deployment, 'build', 'Building Django app')
        assert deployment.metadata_json['current_phase'] == 'build'
        deployment.save.assert_called_once_with(update_fields=['metadata_json'])

    def test_appends_to_phases_list(self):
        deployment = MagicMock()
        deployment.metadata_json = {}
        _set_deploy_phase(deployment, 'starting', 'Init')
        _set_deploy_phase(deployment, 'merge', 'Pulling code')
        _set_deploy_phase(deployment, 'build', 'Building')
        phases = deployment.metadata_json['phases']
        assert len(phases) == 3

    def test_does_not_duplicate_consecutive_phases(self):
        deployment = MagicMock()
        deployment.metadata_json = {}
        _set_deploy_phase(deployment, 'build', 'Building...')
        _set_deploy_phase(deployment, 'build', 'Still building...')
        assert len(deployment.metadata_json['phases']) == 1

    def test_includes_timestamp_and_detail(self):
        deployment = MagicMock()
        deployment.metadata_json = {}
        _set_deploy_phase(deployment, 'merge', 'Pulling code')
        phases = deployment.metadata_json['phases']
        assert 'ts' in phases[0]
        assert phases[0]['detail'] == 'Pulling code'

    def test_preserves_existing_metadata(self):
        deployment = MagicMock()
        deployment.metadata_json = {'existing_key': 'value'}
        _set_deploy_phase(deployment, 'build', 'Building')
        assert deployment.metadata_json['existing_key'] == 'value'
        assert deployment.metadata_json['current_phase'] == 'build'

    def test_handles_none_metadata(self):
        deployment = MagicMock()
        deployment.metadata_json = None
        _set_deploy_phase(deployment, 'starting', 'Init')
        assert deployment.metadata_json['current_phase'] == 'starting'

    def test_swallows_exceptions(self):
        deployment = MagicMock()
        deployment.metadata_json = None
        deployment.save.side_effect = Exception("DB error")
        _set_deploy_phase(deployment, 'build', 'Building')
