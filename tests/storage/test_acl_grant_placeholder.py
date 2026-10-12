import pytest

from openviking.storage.acl import AclLevel, DirectAcl


def test_vikingdb_empty_list_placeholder_decodes_as_no_grants():
    record = {"acl_direct_grants": ["default"], "acl_inherited_grants": ["7:user:*"]}

    assert DirectAcl.from_context_fields(record, "acl_direct").empty
    inherited = DirectAcl.from_context_fields(record, "acl_inherited")
    assert [(entry.principal, entry.level) for entry in inherited.entries] == [
        ("user:*", AclLevel.MANAGE)
    ]


def test_invalid_grant_tokens_still_fail():
    with pytest.raises(RuntimeError, match="Invalid ACL grant token"):
        DirectAcl.from_context_fields({"acl_direct_grants": ["default", "7:user:*"]}, "acl_direct")
