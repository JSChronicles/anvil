import pytest

from anvil.descriptors import OrgDescriptor
from anvil.validators import validate_org_descriptors


def test_duplicate_org_names():
    orgs = [OrgDescriptor(name="a"), OrgDescriptor(name="a")]

    with pytest.raises(ValueError):
        validate_org_descriptors(orgs)
