from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from openviking.session.compressor_v3 import _memory_file_to_case
from openviking.session.memory.dataclass import MemoryFile
from openviking.session.memory.extraction_output_protocol import (
    ExtractionOutputContext,
    create_extraction_output_protocol,
)
from openviking.session.memory.memory_type_registry import (
    MemoryTypeRegistry,
    resolve_memory_templates_dir,
)
from openviking.session.memory.merge_op import MergeOp
from openviking.session.memory.page_id_map import PageIdMap
from openviking.session.memory.schema_model_generator import SchemaModelGenerator
from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils
from openviking.session.memory.utils.uri import generate_uri


@pytest.mark.parametrize("language", ["en", "zh-CN"])
@pytest.mark.parametrize("domain_name", ["airline_customer_service", "library_services"])
def test_case_schema_groups_business_domains_without_changing_storage_contract(
    language, domain_name
):
    registry = MemoryTypeRegistry(load_schemas=False)
    registry.load_from_yaml(str(resolve_memory_templates_dir() / "cases.yaml"))
    schema = registry.get("cases")
    generator = SchemaModelGenerator([schema], template_context={"language": language})
    model = generator.create_flat_data_model(schema)
    operations = generator.create_structured_operations_model()
    description = operations.model_fields["cases"].description
    assert "business domain" in description
    assert "Infer the business domain from the current service/task context" in description
    assert "one Case: airline_customer_service" not in description
    assert "Do not split a domain" in description
    assert "sdk.commit(cases=[loaded_case])" in description
    assert "selected_case_ids" in operations.model_fields
    assert "Do not group tasks merely because they share a broad domain" not in description
    assert "{{ language }}" not in description
    name_description = model.model_fields["case_name"].description
    assert "regardless of conversation language" in name_description
    assert "Examples only, not prescribed identifiers or a fixed domain list" in name_description
    assert {field.name for field in schema.fields} == {
        "case_name",
        "task_signature",
        "input",
        "rubric",
    }
    assert {field.name: field.merge_op for field in schema.fields} == {
        "case_name": MergeOp.IMMUTABLE,
        "task_signature": MergeOp.IMMUTABLE,
        "input": MergeOp.IMMUTABLE,
        "rubric": MergeOp.PATCH,
    }
    fields = {
        "case_name": domain_name,
        "task_signature": f"Service scope for {domain_name}.",
        "input": f"Requests in {domain_name} use the current policy and supplied records.",
        "rubric": '[{"id":"confirm","description":"Confirm changes before execution."}]',
    }
    parsed = model.model_validate({"page_id": 100, **fields})
    assert parsed.case_name == fields["case_name"]
    assert generate_uri(schema, fields, user_space="default") == (
        f"viking://user/default/memories/cases/{domain_name}.md"
    )


@pytest.mark.parametrize("output_format", ["python", "json"])
def test_case_rubric_round_trips_through_protocol_template_and_learning(output_format):
    registry = MemoryTypeRegistry(load_schemas=False)
    registry.load_from_yaml(str(resolve_memory_templates_dir() / "cases.yaml"))
    schema = registry.get("cases")
    context = ExtractionOutputContext(
        operations_model=SchemaModelGenerator([schema]).create_structured_operations_model(),
        schemas=(schema,),
        page_id_map=PageIdMap(),
        read_file_contents={},
        link_enabled=False,
    )
    fields = {
        "case_name": "library_services",
        "task_signature": "办理图书借还、续借和预约请求。",
        "input": "基于当前借阅规则和读者提供的信息处理请求。\n保留工具字段的原始值。",
        "rubric": json.dumps(
            [
                {"id": "scope", "description": "不超出读者授权的范围。"},
                {"id": "confirm", "description": "修改借阅记录前确认操作明细。"},
            ],
            ensure_ascii=False,
        ),
    }
    protocol = create_extraction_output_protocol(output_format)
    contract = protocol.render_contract(context)
    assert "Plain-text domain context" in contract
    assert "JSON list of domain-wide evaluation criteria" in contract
    assert '"required":true' not in contract
    if output_format == "python":
        program = (
            "sdk.create_cases("
            + ", ".join(f"{name}={value!r}" for name, value in fields.items())
            + ")\nsdk.commit()"
        )
    else:
        program = json.dumps({"cases": [{"page_id": 100, **fields}]}, ensure_ascii=False)
    operations, error = protocol.parse(program, context)
    assert error is None
    item = operations.cases[0]
    assert item.input == fields["input"] and item.rubric == fields["rubric"]
    uri = generate_uri(schema, fields, user_space="default")
    raw = MemoryFileUtils.write(
        MemoryFile(
            uri=uri,
            memory_type="cases",
            content="STALE_MODEL_WRITTEN_BODY",
            extra_fields=fields,
        ),
        content_template=schema.content_template,
    )
    memory = MemoryFileUtils.read(raw, uri=uri)
    assert "STALE_MODEL_WRITTEN_BODY" not in memory.content
    assert f"## Domain Context\n{fields['input']}" in memory.content
    assert f"## Rubric\n```json\n{fields['rubric']}" in memory.content
    assert memory.extra_fields["input"] == fields["input"]
    assert memory.extra_fields["rubric"] == fields["rubric"]
    case = _memory_file_to_case(memory)
    assert case.input == {"summary": fields["input"]}
    assert case.rubric.description == ""
    assert [
        {"id": c.name, "description": c.description} for c in case.rubric.criteria
    ] == json.loads(fields["rubric"])
    serialized = asdict(case)
    assert len(serialized["rubric"]["criteria"]) == 2
    assert serialized["metadata"]["memory_fields"]["rubric"] == fields["rubric"]


def test_missing_rubric_does_not_invent_default_criteria():
    case = _memory_file_to_case(
        MemoryFile(
            uri="viking://user/default/memories/cases/schema_authoring.md",
            memory_type="cases",
            content="",
            extra_fields={"case_name": "schema_authoring"},
        )
    )
    assert case.rubric.description == ""
    assert case.rubric.criteria == []
