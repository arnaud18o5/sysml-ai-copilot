from sysml_copilot.workspace import Workspace

from .conftest import SAMPLE


def codes(ws, severity=None):
    return [d.code for d in ws.diagnostics() if severity is None or d.severity == severity]


def edges(ws, type_):
    return sorted((ws.id_of(e.source), ws.id_of(e.target)) for e in ws.edges() if e.type == type_)


def test_sample_is_clean_and_connects_through_types():
    ws = Workspace.load(SAMPLE)
    assert codes(ws, "error") == [] and codes(ws, "warning") == []
    assert edges(ws, "CONNECTS_TO") == [
        ("VehicleFuelSystem.FuelPump.fuelOut", "VehicleFuelSystem.Engine.fuelIn"),
        ("VehicleFuelSystem.FuelTank.fuelOut", "VehicleFuelSystem.FuelPump.fuelIn"),
    ]
    # built-in scalar types resolve
    assert ("VehicleFuelSystem.FuelTank.capacity", "ScalarValues.Real") in edges(ws, "TYPED_BY")


def test_feature_chain_lookup():
    ws = Workspace.load(SAMPLE)
    res = ws.lookup("VehicleFuelSystem.Vehicle.tank.fuelOut")
    assert res.ok and ws.id_of(res.target) == "VehicleFuelSystem.FuelTank.fuelOut"
    assert res.hops[-1] == "inherited"


def test_imports_and_inheritance():
    ws = Workspace.from_text("""
package Lib {
    part def Engine { port shaft : Shaft; }
    port def Shaft;
}
package App {
    import Lib::*;
    part def Base { part engine : Engine; }
    part def Car :> Base {
        part gearbox : Gearbox;
        connect engine.shaft to gearbox.input;
    }
    part def Gearbox { port input : Lib::Shaft; }
}
""")
    assert codes(ws, "error") == []
    assert edges(ws, "CONNECTS_TO") == [("Lib.Engine.shaft", "App.Gearbox.input")]
    assert edges(ws, "SPECIALIZES") == [("App.Car", "App.Base")]


def test_unresolved_reference_with_suggestion():
    ws = Workspace.from_text("package P { part def Engine; part e : Engnie; }")
    diag = next(d for d in ws.diagnostics() if d.code == "unresolved_reference")
    assert diag.element == "P.e" and "P.Engine" in diag.suggestions


def test_heuristic_and_ambiguous_references():
    ws = Workspace.from_text("""package P {
    part def A { part x; }
    part def B { part y; part z; }
    part def C { part z; }
    requirement def R;
    satisfy R by x;
    satisfy R by z;
}""")
    diags = {d.code: d for d in ws.diagnostics()}
    assert diags["heuristic_reference"].suggestions == ["A::x"]
    assert set(diags["ambiguous_reference"].suggestions) == {"P.B.z", "P.C.z"}


def test_typing_checks_and_cycles():
    ws = Workspace.from_text("""package P {
    part def A :> B;
    part def B :> A;
    port def Q;
    part def Box { part p : Q; port q : Box; part u : Box.p; }
    requirement def R;
    satisfy Box by Box.p;
}""")
    found = codes(ws)
    assert found.count("specialization_cycle") == 2
    assert "typing_kind_mismatch" in found  # part typed by a port def / port by a part def
    assert "typed_by_non_definition" in found
    assert "satisfy_non_requirement" in found


def test_case_is_compatible_with_calc_typing():
    # Case :> Calculation in the standard library: a generic `calc` usage
    # may be typed by the more specific `case def` (no semantics lost), but
    # not the other way around (a `case` usage asking for calc-def typing
    # would lose case-specific meaning) - same asymmetry as action/calc.
    ws = Workspace.from_text("""package P {
    calc def Calc1;
    case def Case1;
    part holder {
        calc c : Case1;
    }
}""")
    assert "typing_kind_mismatch" not in codes(ws)

    ws2 = Workspace.from_text("""package P {
    calc def Calc1;
    part holder {
        case x : Calc1;
    }
}""")
    assert "typing_kind_mismatch" in codes(ws2)


def test_relationship_metadata_is_resolved_and_rewritten_on_rename():
    ws = Workspace.from_text("""package P {
    metadata def Tag;
    part def A { port p; }
    part def B { port q; }
    part a : A;
    part b : B;
    #Tag connect a.p to b.q;
}""")
    assert codes(ws, "error") == []
    rel = next(n for n in ws.nodes() if type(n).__name__ == "Relationship")
    assert str(rel.metadata[0]) == "Tag"


def test_duplicates_are_reported_not_silently_merged():
    ws = Workspace.from_text("package P { part def A; part def A; }")
    assert "duplicate_name" in codes(ws, "error")


def test_alias_and_redefinition_resolution():
    ws = Workspace.from_text("""package P {
    alias Engine2 for Engine;
    part def Engine { attribute mass : Real; }
    part def Light :> Engine {
        attribute :>> mass = 900;
    }
    part e : Engine2;
    comment about Engin /* typo */
}""")
    assert ("P.e", "P.Engine") in edges(ws, "TYPED_BY")
    assert ("P.Light.mass", "P.Engine.mass") in edges(ws, "REDEFINES")
    unresolved = [d for d in ws.diagnostics() if d.code == "unresolved_reference"]
    assert len(unresolved) == 1 and "Engin" in unresolved[0].message
