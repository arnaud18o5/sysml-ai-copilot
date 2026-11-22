import pytest

from sysml_copilot.model import Element, Import, Relationship
from sysml_copilot.syntax import (
    SysmlSyntaxError,
    parse_document,
    parse_ref,
    parse_value,
    serialize_document,
)

from .conftest import SAMPLE


def roundtrip(text):
    doc = parse_document(text)
    out = serialize_document(doc)
    assert serialize_document(parse_document(out)) == out, "serialization must be idempotent"
    return doc, out


def test_sample_is_canonical():
    text = open(SAMPLE, encoding="utf-8").read()
    assert serialize_document(parse_document(text)) == text


def test_rich_constructs_roundtrip():
    text = """
package 'Système' {
    private import ScalarValues::*;
    import *;

    abstract part def Base;

    part def Véhicule :> Base {
        doc /* Top-level system. */

        in port ctrl : Ctrl;
        part wheels : Wheel[4];
        part spare : Wheel[0..1];
        attribute mass : Real = 1200 [kg];
        attribute name : String = "car \\"A\\"";
        attribute electric : Boolean = false;
        attribute ref : Real = Base.x;
        part 'front axle';
    }

    part def Light :> Véhicule {
        attribute :>> mass = 900;
    }

    port def Ctrl {
        in item cmd;
    }

    part def Wheel;
    requirement def R1;
    use case def Drive;
    verification case def VerifyR1;
    enum def Color {
        enum red;
        enum green;
    }

    satisfy requirement R1 by Véhicule.wheels;
    allocate Drive to Véhicule;
}

package Other;
"""
    doc, out = roundtrip(text)
    pkg = doc.members[0]
    assert pkg.name == "Système"
    assert isinstance(pkg.members[0], Import) and pkg.members[0].visibility == "private"
    assert pkg.members[1].path == [] and pkg.members[1].wildcard == "*"
    veh = next(m for m in pkg.members if isinstance(m, Element) and m.name == "Véhicule")
    assert veh.doc == "Top-level system."
    assert [str(r) for r in veh.specializes] == ["Base"]
    feats = {m.effective_name: m for m in veh.element_members()}
    assert feats["ctrl"].direction == "in"
    assert feats["wheels"].multiplicity == "4"
    assert feats["spare"].multiplicity == "0..1"
    light = next(m for m in pkg.members if isinstance(m, Element) and m.name == "Light")
    assert light.members[0].name is None and light.members[0].effective_name == "mass"
    assert "attribute :>> mass = 900;" in out
    assert feats["name"].value.text == 'car "A"'
    assert feats["front axle"].name == "front axle"
    kinds = {m.name: m.kind for m in pkg.members if isinstance(m, Element)}
    assert kinds["Drive"] == "UseCaseDefinition"
    assert kinds["VerifyR1"] == "VerificationCaseDefinition"
    assert kinds["Color"] == "EnumerationDefinition"
    rels = [m for m in pkg.members if isinstance(m, Relationship)]
    assert [(r.kind, str(r.source), str(r.target)) for r in rels] == [
        ("satisfy", "Véhicule.wheels", "R1"),
        ("allocate", "Drive", "Véhicule"),
    ]
    assert "'front axle'" in out
    assert "package Other {" in out


def test_quoted_name_escapes():
    doc, out = roundtrip("package P { part def 'it\\'s'; }")
    assert doc.members[0].members[0].name == "it's"
    assert "'it\\'s'" in out


def test_javadoc_doc_has_no_stray_stars():
    doc, _ = roundtrip("""package P {
    requirement def R {
        doc /**
         * Line one.
         * Line two.
         **/
    }
}""")
    assert doc.members[0].members[0].doc == "Line one.\nLine two."


def test_comments_are_ignored():
    doc, _ = roundtrip("""// header
package P { /* floating */ part def A; // trailing
}""")
    assert doc.members[0].members[0].name == "A"


def test_keywords_are_not_names_but_prefixes_are():
    doc, _ = roundtrip("package P { part def partner; part def importer; }")
    assert [m.name for m in doc.members[0].members] == ["partner", "importer"]


def test_syntax_error_has_position():
    with pytest.raises(SysmlSyntaxError) as info:
        parse_document("package P {\n    part def A\n}", "x.sysml")
    err = info.value
    assert err.line == 3 and err.path == "x.sysml"
    assert err.to_json()["code"] == "syntax_error"


def test_unclosed_block():
    with pytest.raises(SysmlSyntaxError) as info:
        parse_document("package P {")
    assert "end of input" in str(info.value)


def test_wildcard_only_last_in_import():
    with pytest.raises(SysmlSyntaxError):
        parse_document("package P { import A::*::B; }")


def test_parse_ref_and_value():
    assert parse_ref("A::B.'c d'").segments == ["A", "B", "c d"]
    v = parse_value("3.5 [bar]")
    assert v.kind == "number" and v.text == "3.5" and str(v.unit) == "bar"
    assert parse_value('"x"').kind == "string"
    assert parse_value("true").kind == "boolean"
    assert parse_value("A.b").kind == "ref"
