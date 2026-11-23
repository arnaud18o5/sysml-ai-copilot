import pytest

from sysml_copilot.model import Element, Import, Relationship
from sysml_copilot.syntax import (
    SysmlSyntaxError,
    parse_document,
    parse_ref,
    parse_value,
    serialize_document,
    tree_signature,
)

from .conftest import SAMPLE


def roundtrip(text):
    doc = parse_document(text)
    out = serialize_document(doc)
    again = parse_document(out)
    assert tree_signature(again) == tree_signature(doc), "serialization must not lose information"
    assert serialize_document(again) == out, "serialization must be idempotent"
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
    verification def VerifyR1;
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


def test_declaration_features_roundtrip():
    text = """standard library package Lib {
    private import ScalarValues::*;
    public alias Num for ScalarValues::Real;
    comment about Base /* Explains Base. */
    comment Note /* A named note. */

    abstract part def <B> Base;
    metadata def Safety;
    port def P;

    #Safety part def Car :> Base {
        protected attribute :>> mass : Real = 1200 [kg];
        attribute wheels : Num[4] ordered nonunique;
        in ref item fuel :: > x;
        end #Safety part bead : Base[1];
        variant part sports : Base;
        port p : ~P;
        derived readonly attribute total : Real default = 3;
        attribute speed : Real := 0;
        part x :> Base;
        ref r ::> x;
        metadata m : Safety about x;
        @Safety;
    }

    verification def Test;
    individual part def Unit1 :> Car;
}

library package L2;
"""
    text = text.replace(":: > x", ":> x")
    doc, out = roundtrip(text)
    pkg = doc.members[0]
    assert pkg.library_kind == "standard library" and pkg.kind == "LibraryPackage"
    imp, alias, about_comment, note = pkg.members[:4]
    assert imp.visibility == "private"
    assert alias.name == "Num" and alias.visibility == "public"
    assert [str(r) for r in about_comment.about] == ["Base"] and about_comment.text == "Explains Base."
    assert note.name == "Note"
    els = {m.effective_name: m for m in pkg.members if isinstance(m, Element)}
    assert els["Base"].abstract and els["Base"].short_name == "B"
    assert els["Safety"].kind == "MetadataDefinition"
    car = els["Car"]
    assert [str(m) for m in car.metadata] == ["Safety"]
    f = {m.effective_name: m for m in car.members if isinstance(m, Element) and m.effective_name}
    assert f["mass"].visibility == "protected" and f["mass"].name is None
    assert f["wheels"].multiplicity_props == ["ordered", "nonunique"]
    assert f["fuel"].direction == "in" and f["fuel"].is_ref
    assert f["bead"].modifiers == ["end"] and [str(m) for m in f["bead"].metadata] == ["Safety"]
    assert f["sports"].modifiers == ["variant"]
    assert f["p"].types[0].conjugated
    assert f["total"].modifiers == ["readonly", "derived"] and f["total"].value_op == "default ="
    assert f["speed"].value_op == ":="
    assert f["r"].kind == "ReferenceUsage" and [str(r) for r in f["r"].references] == ["x"]
    assert f["m"].kind == "MetadataUsage" and [str(r) for r in f["m"].about] == ["x"]
    shorthand = [m for m in car.members if isinstance(m, Element) and m.at_shorthand]
    assert len(shorthand) == 1 and str(shorthand[0].types[0]) == "Safety"
    assert els["Test"].kind == "VerificationCaseDefinition"
    assert els["Unit1"].modifiers == ["individual"]
    assert "port p : ~P;" in out and "@Safety;" in out and "library package L2 {" in out


def test_expression_values_roundtrip():
    text = """
package Expr {
    part def Vehicle {
        attribute mass : Real;
        attribute chassisMass : Real;
        attribute engineMass : Real;
    }

    part vehicle : Vehicle {
        attribute totalMass : Real = chassisMass + engineMass + 2 * (vehicle.mass - 1);
        attribute negated : Real = -vehicle.mass;
        attribute ok : Boolean = not (vehicle.mass > 0);
        attribute scaled : Real = vehicle->size(2, vehicle.mass)->first();
        attribute items : Real = (1, 2, 3);

        constraint {
            totalMass == chassisMass + engineMass and vehicle.mass > 0
        }
    }
}
"""
    doc, out = roundtrip(text)
    pkg = doc.members[0]
    vehicle = next(m for m in pkg.members if isinstance(m, Element) and m.effective_name == "vehicle")
    f = {m.effective_name: m for m in vehicle.members if isinstance(m, Element) and m.effective_name}

    assert f["totalMass"].value.kind == "expr"
    assert f["totalMass"].value.expr.op == "add"
    assert f["negated"].value.expr.op == "neg"
    assert f["ok"].value.expr.op == "not"
    assert f["scaled"].value.expr.op == "arrow" and f["scaled"].value.expr.name == "first"
    assert f["items"].value.expr.op == "seq" and len(f["items"].value.expr.args) == 3

    constraint = next(m for m in vehicle.members if isinstance(m, Element) and m.keyword == "constraint")
    [result] = [m for m in constraint.members if not isinstance(m, Element)]
    assert result.expr.op == "and"

    # Refs embedded in expressions are exposed through refs() for resolution
    # and rename/move rewriting, same as any other reference.
    refs = {str(r) for r in f["totalMass"].value.expr.refs()}
    assert refs == {"chassisMass", "engineMass", "vehicle.mass"}
    assert "totalMass == chassisMass + engineMass and vehicle.mass > 0" in out
    assert "2 * (vehicle.mass - 1)" in out


def test_expression_precedence_round_trips_minimal_parens():
    doc, out = roundtrip("package P { attribute a : Real = 1 - 2 - 3; }")
    e = doc.members[0].members[0].value.expr
    assert e.op == "sub" and e.args[0].op == "sub"
    assert "1 - 2 - 3" in out

    doc2, out2 = roundtrip("package P { attribute a : Real = 1 - (2 - 3); }")
    e2 = doc2.members[0].members[0].value.expr
    assert e2.args[1].op == "sub"
    assert "1 - (2 - 3)" in out2


def test_modifier_misuse_is_rejected():
    with pytest.raises(SysmlSyntaxError):
        parse_document("package P { in part def A; }")
    with pytest.raises(SysmlSyntaxError):
        parse_document("abstract package P;")
    with pytest.raises(SysmlSyntaxError):
        parse_document("package P { abstract import X::*; }")
