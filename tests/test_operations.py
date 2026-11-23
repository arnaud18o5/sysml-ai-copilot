import os

from sysml_copilot.operations import ModelService, call

from .conftest import PKG, ok


def src(svc):
    return svc.get_source()["documents"][0]["source"]


def test_create_typed_usage_and_reject_bad_input(svc):
    r = ok(call(svc, "create_element", {"kind": "part def", "name": "Filter", "owner": PKG,
                                        "doc": "Removes contaminants"}))
    assert r["created"] == f"{PKG}.Filter" and r["validation"]["errors"] == 0
    ok(call(svc, "create_element", {"kind": "port", "name": "inlet", "owner": f"{PKG}.Filter",
                                    "type": f"{PKG}.FuelPort", "direction": "in"}))
    ok(call(svc, "create_element", {"kind": "part", "name": "filter", "owner": f"{PKG}.Vehicle",
                                    "type": f"{PKG}.Filter", "multiplicity": "0..1"}))
    assert "part filter : Filter[0..1];" in src(svc)

    bad = call(svc, "create_element", {"kind": "port", "name": "x", "owner": f"{PKG}.Vehicle",
                                       "type": f"{PKG}.Engine"})
    assert bad["error"]["code"] == "incompatible_type"
    bad = call(svc, "create_element", {"kind": "part", "name": "filter", "owner": f"{PKG}.Vehicle"})
    assert bad["error"]["code"] == "duplicate_name"
    bad = call(svc, "create_element", {"kind": "prat def", "name": "Y", "owner": PKG})
    assert bad["error"]["code"] == "invalid_kind" and "part def" in bad["error"]["suggestions"]
    bad = call(svc, "create_element", {"kind": "part", "name": "y", "owner": f"{PKG}.Vehicel"})
    assert bad["error"]["code"] == "not_found" and f"{PKG}.Vehicle" in bad["error"]["suggestions"]


def test_values(svc):
    owner = f"{PKG}.Vehicle"
    ok(call(svc, "create_element", {"kind": "attribute", "name": "mass", "owner": owner, "type": "Real",
                                    "value": "1200 [kg]"}))
    ok(call(svc, "create_element", {"kind": "attribute", "name": "label", "owner": owner, "type": "String",
                                    "value": "tank A"}))
    ok(call(svc, "create_element", {"kind": "attribute", "name": "on", "owner": owner, "type": "Boolean",
                                    "value": True}))
    ok(call(svc, "create_element", {"kind": "attribute", "name": "cap", "owner": owner, "type": "Real",
                                    "value": f"{PKG}.FuelTank.capacity", "value_kind": "ref"}))
    text = src(svc)
    for line in ('attribute mass : Real = 1200 [kg];', 'attribute label : String = "tank A";',
                 'attribute on : Boolean = true;', 'attribute cap : Real = FuelTank::capacity;'):
        assert line in text
    assert call(svc, "update_element", {"element": f"{owner}.mass", "clear": ["value"]})["ok"]
    assert "attribute mass : Real;" in src(svc)


def test_rename_rewrites_all_references(svc):
    r = ok(call(svc, "update_element", {"element": f"{PKG}.FuelTank", "name": "Fuel Tank"}))
    assert r["updated"] == f"{PKG}.'Fuel Tank'"
    r = ok(call(svc, "update_element", {"element": f"{PKG}.Vehicle.tank", "name": "mainTank"}))
    text = src(svc)
    assert "part mainTank : 'Fuel Tank';" in text
    assert "connect mainTank.fuelOut to pump.fuelIn;" in text
    assert "satisfy FuelPurityRequirement by Vehicle::mainTank;" in text
    assert svc.validate()["errors"] == 0


def test_move_rewrites_references(svc):
    ok(call(svc, "create_element", {"kind": "package", "name": "Parts", "owner": PKG}))
    r = ok(call(svc, "move_element", {"element": f"{PKG}.FuelPump", "new_owner": f"{PKG}.Parts"}))
    assert r["moved"] == f"{PKG}.Parts.FuelPump"
    assert "part pump : Parts::FuelPump;" in src(svc)
    assert svc.validate()["errors"] == 0
    bad = call(svc, "move_element", {"element": f"{PKG}.Parts", "new_owner": f"{PKG}.Parts.FuelPump"})
    assert bad["error"]["code"] == "invalid_owner"


def test_delete_requires_cascade_when_referenced(svc):
    r = call(svc, "delete_element", {"element": f"{PKG}.FuelPump"})
    assert r["error"]["code"] == "has_dependents"
    assert any("pump" in d["in"] for d in r["error"]["details"])
    r = ok(call(svc, "delete_element", {"element": f"{PKG}.FuelPump", "cascade": True}))
    text = src(svc)
    assert "part def FuelPump" not in text
    assert "part pump;" in text  # typing removed
    assert "connect tank.fuelOut to pump.fuelIn" not in text  # connection to its port removed
    assert svc.validate()["errors"] == 0


def test_inherited_feature_cannot_be_edited_through_chain(svc):
    r = call(svc, "update_element", {"element": f"{PKG}.Vehicle.tank.fuelOut", "name": "out"})
    assert r["error"]["code"] == "inherited_feature"
    assert r["error"]["suggestions"] == [f"{PKG}.FuelTank.fuelOut"]


def test_inherited_features_skip_unnamed_members(svc):
    # Vehicle owns an unnamed member (a bare `constraint { ... }` usage)
    # and Diagnose.vehicle is typed by it - walking that inheritance chain
    # must not choke on a member with no name to inherit.
    el = svc.get_element(f"{PKG}.Diagnose.vehicle")
    assert any(f["name"] == "capacityMargin" for f in el["inherited_features"])
    svc.export_model()


def test_relationships(svc):
    r = ok(call(svc, "add_relationship", {"kind": "connect", "source": f"{PKG}.Vehicle.tank.fuelOut",
                                          "target": f"{PKG}.Vehicle.engine.fuelIn"}))
    assert r["created"]["owner"] == f"{PKG}.Vehicle"
    assert r["created"]["text"] == "connect tank.fuelOut to engine.fuelIn;"
    dup = call(svc, "add_relationship", {"kind": "connect", "source": f"{PKG}.Vehicle.tank.fuelOut",
                                         "target": f"{PKG}.Vehicle.engine.fuelIn"})
    assert dup["error"]["code"] == "already_exists"
    bad = call(svc, "add_relationship", {"kind": "satisfy", "source": f"{PKG}.Vehicle.pump",
                                         "target": f"{PKG}.Engine"})
    assert bad["error"]["code"] == "invalid_endpoint"
    ok(call(svc, "add_relationship", {"kind": "allocate", "source": f"{PKG}.FuelDeliveryRequirement",
                                      "target": f"{PKG}.Vehicle.pump"}))
    ok(call(svc, "remove_relationship", {"kind": "connect", "source": f"{PKG}.Vehicle.tank.fuelOut",
                                         "target": f"{PKG}.Vehicle.engine.fuelIn"}))
    assert "connect tank.fuelOut to engine.fuelIn" not in src(svc)
    ok(call(svc, "remove_relationship", {"kind": "satisfy", "source": f"{PKG}.Vehicle.pump",
                                         "target": f"{PKG}.FuelDeliveryRequirement"}))
    trace = svc.requirements_trace()
    assert trace["satisfied"] == 1 and trace["total"] == 2


def test_batch_is_atomic_and_undo_redo(svc):
    before = src(svc)
    r = call(svc, "apply_batch", {"operations": [
        {"op": "create_element", "args": {"kind": "part def", "name": "X", "owner": PKG}},
        {"op": "create_element", "args": {"kind": "part", "name": "x", "owner": f"{PKG}.Nope"}},
    ]})
    assert r["error"]["code"] == "batch_failed" and r["error"]["details"]["index"] == 1
    assert src(svc) == before
    r = ok(call(svc, "apply_batch", {"operations": [
        {"op": "create_element", "args": {"kind": "part def", "name": "X", "owner": PKG}},
        {"op": "create_element", "args": {"kind": "part", "name": "x", "owner": f"{PKG}.Vehicle",
                                          "type": f"{PKG}.X"}},
    ]}))
    assert len(r["results"]) == 2
    after = src(svc)
    ok(call(svc, "undo"))
    assert src(svc) == before
    ok(call(svc, "redo"))
    assert src(svc) == after
    assert call(svc, "redo")["error"]["code"] == "nothing_to_redo"


def test_add_from_text(svc):
    r = ok(call(svc, "add_from_text", {"owner": PKG, "text": "part def Wheel { attribute radius : Real; }"}))
    assert r["created"] == [f"{PKG}.Wheel"]
    bad = call(svc, "add_from_text", {"owner": PKG, "text": "part def Hub { part w : Wheell; }"})
    assert bad["error"]["code"] == "introduces_errors"
    bad = call(svc, "add_from_text", {"owner": PKG, "text": "part def {"})
    assert bad["error"]["code"] == "syntax_error"
    assert "Hub" not in src(svc)


def test_save_diff_and_autosave(sample_path):
    svc = ModelService([sample_path])
    ok(call(svc, "create_element", {"kind": "part def", "name": "Z", "owner": PKG}))
    assert "+    part def Z;" in svc.diff()["diff"]
    assert svc.save()["written"] == [sample_path]
    assert "part def Z;" in open(sample_path, encoding="utf-8").read()
    auto = ModelService([sample_path], autosave=True)
    r = ok(call(auto, "delete_element", {"element": f"{PKG}.Z"}))
    assert r["saved"] == [sample_path]
    assert "part def Z;" not in open(sample_path, encoding="utf-8").read()


def test_multi_document_workspace(tmp_path, sample_path):
    svc = ModelService([os.path.dirname(sample_path)])
    path = str(tmp_path / "library.sysml")
    ok(call(svc, "create_document", {"path": path, "package": "Library"}))
    ok(call(svc, "create_element", {"kind": "part def", "name": "Sensor", "owner": "Library"}))
    ok(call(svc, "create_element", {"kind": "part", "name": "sensor", "owner": f"{PKG}.Vehicle",
                                    "type": "Library.Sensor"}))
    assert "part sensor : Library::Sensor;" in svc.get_source(element=f"{PKG}.Vehicle")["source"]
    svc.save()
    reloaded = ModelService([os.path.dirname(sample_path)])
    assert reloaded.validate()["errors"] == 0


def test_queries(svc):
    found = svc.find_elements("fuel pump")["results"]
    assert found[0]["id"] == f"{PKG}.FuelPump"
    impact = svc.impact_analysis(f"{PKG}.FuelPump")
    ids = [i["id"] for i in impact["impacted"]]
    assert ids[0] == f"{PKG}.Vehicle.pump"
    assert f"{PKG}.FuelDeliveryRequirement" in ids and f"{PKG}.Vehicle.engine" in ids
    assert all(i["because"] for i in impact["impacted"])
    deps = svc.impact_analysis(f"{PKG}.Vehicle.pump", direction="dependencies", max_hops=1)
    assert f"{PKG}.FuelPump" in [i["id"] for i in deps["impacted"]]
    assert call(svc, "impact_analysis", {"element": PKG, "max_hops": "3; DROP"})["error"]["code"] == "invalid_argument"
    el = svc.get_element(f"{PKG}.Vehicle.tank")
    assert {f["name"] for f in el["inherited_features"]} == {"fuelOut", "capacity", "fuelType", "calibrationPoints"}
    tree = svc.get_tree(depth=1)
    assert tree["documents"][0]["roots"][0]["id"] == PKG
    exported = svc.export_model()
    assert len(exported["elements"]) == svc.summary()["element_count"]


def test_unknown_operation():
    r = call(ModelService(), "creat_element", {})
    assert r["error"]["code"] == "unknown_operation" and "create_element" in r["error"]["suggestions"]


def test_two_sessions_on_the_same_files(sample_path):
    """An agent (MCP) and the web UI may edit the same files concurrently."""
    agent = ModelService([sample_path], autosave=True)
    ui = ModelService([sample_path], autosave=True)
    v0 = call(ui, "summary")["version"]
    ok(call(agent, "create_element", {"kind": "part def", "name": "FromAgent", "owner": PKG}))
    seen = call(ui, "get_element", {"element": f"{PKG}.FromAgent"})
    assert seen["ok"] and call(ui, "summary")["version"] > v0
    ok(call(ui, "create_element", {"kind": "part def", "name": "FromUi", "owner": PKG}))
    assert call(agent, "get_element", {"element": f"{PKG}.FromUi"})["ok"]

    # unsaved local work + external change -> writes are refused, not silently merged
    manual = ModelService([sample_path], autosave=False)
    ok(call(manual, "create_element", {"kind": "part def", "name": "Draft", "owner": PKG}))
    ok(call(agent, "delete_element", {"element": f"{PKG}.FromUi"}))
    r = call(manual, "create_element", {"kind": "part def", "name": "Draft2", "owner": PKG})
    assert r["error"]["code"] == "conflict"
    ok(call(manual, "reload"))
    assert call(manual, "get_element", {"element": f"{PKG}.FromUi"})["error"]["code"] == "not_found"
