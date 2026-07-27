import json
from enum import Enum

import pytest

from src.core.error_taxonomy import ErrorCategory
from src.core.provider_circuit_breaker import CircuitState
from src.core.state_machine import RecordingState
from src.data.job_store import JobStatus, JobType
from src.data.meeting_import_store import MeetingImportStatus
from src.data.transcript_artifact_store import AlignmentQuality, AttemptState, SourceAssetState


@pytest.mark.parametrize(
    "enum_type",
    [
        ErrorCategory,
        CircuitState,
        RecordingState,
        JobType,
        JobStatus,
        MeetingImportStatus,
        AttemptState,
        AlignmentQuality,
        SourceAssetState,
    ],
)
def test_string_enum_compatibility_contract(enum_type: type[Enum]) -> None:
    for member in enum_type:
        legacy_text = f"{enum_type.__name__}.{member.name}"
        assert isinstance(member, str)
        assert isinstance(member.value, str)
        assert str(member) == legacy_text
        assert f"{member}" == legacy_text
        assert member == member.value
        assert json.dumps(member) == json.dumps(member.value)
