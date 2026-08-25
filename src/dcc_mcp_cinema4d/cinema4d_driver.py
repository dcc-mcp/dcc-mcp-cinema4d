"""Package-owned c4dpy entry point. This module runs inside Cinema 4D's Python."""

import json
import math
import os
import sys
import time


def _vector(value, name, positive=False):
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError("%s must contain exactly three numbers" % name)
    result = [float(item) for item in value]
    if not all(math.isfinite(item) for item in result):
        raise ValueError("%s values must be finite" % name)
    if positive and not all(item > 0 for item in result):
        raise ValueError("%s values must be positive" % name)
    return result


def _positive(value, name, allow_zero=False):
    number = float(value)
    if not math.isfinite(number) or (number < 0 if allow_zero else number <= 0):
        kind = "non-negative" if allow_zero else "positive"
        raise ValueError("%s must be a finite %s number" % (name, kind))
    return number


def _as_vector(c4d, values):
    return c4d.Vector(values[0], values[1], values[2])


def _vector_payload(value):
    return [float(value.x), float(value.y), float(value.z)]


def _version_string(value):
    if isinstance(value, (tuple, list)):
        return ".".join(str(item) for item in value)
    return str(value)


def _walk_objects(doc):
    count = [0]

    def walk(node, parent_path, depth):
        if depth > 64:
            raise RuntimeError("Object hierarchy exceeds the 64-level limit")
        while node is not None:
            count[0] += 1
            if count[0] > 10000:
                raise RuntimeError("Document exceeds the 10000-object inspection limit")
            name = str(node.GetName())
            path = "%s/%s" % (parent_path, name) if parent_path else name
            yield node, path, depth
            child = node.GetDown()
            if child is not None:
                for item in walk(child, path, depth + 1):
                    yield item
            node = node.GetNext()

    first = doc.GetFirstObject()
    if first is not None:
        for item in walk(first, "", 0):
            yield item


def _object_payload(obj, path, depth):
    position = obj.GetAbsPos()
    rotation = obj.GetAbsRot()
    scale = obj.GetAbsScale()
    midpoint = obj.GetMp()
    radius = obj.GetRad()
    payload = {
        "name": str(obj.GetName()),
        "path": path,
        "depth": depth,
        "type_id": int(obj.GetType()),
        "type_name": str(obj.GetTypeName()),
        "transform": {
            "translation": _vector_payload(position),
            "rotation_hpb_degrees": [
                math.degrees(float(rotation.x)),
                math.degrees(float(rotation.y)),
                math.degrees(float(rotation.z)),
            ],
            "scale": _vector_payload(scale),
        },
        "bounds": {
            "midpoint": _vector_payload(midpoint),
            "radius": _vector_payload(radius),
            "min": _vector_payload(midpoint - radius),
            "max": _vector_payload(midpoint + radius),
        },
        "child_count": int(obj.GetChildren().__len__()),
    }
    get_point_count = getattr(obj, "GetPointCount", None)
    get_polygon_count = getattr(obj, "GetPolygonCount", None)
    if callable(get_point_count):
        payload["point_count"] = int(get_point_count())
    if callable(get_polygon_count):
        payload["polygon_count"] = int(get_polygon_count())
    return payload


def _material_payload(material):
    return {
        "name": str(material.GetName()),
        "type_id": int(material.GetType()),
        "type_name": str(material.GetTypeName()),
    }


def _document_payload(doc):
    objects = [_object_payload(obj, path, depth) for obj, path, depth in _walk_objects(doc)]
    materials = []
    material = doc.GetFirstMaterial()
    while material is not None:
        if len(materials) >= 10000:
            raise RuntimeError("Document exceeds the 10000-material inspection limit")
        materials.append(_material_payload(material))
        material = material.GetNext()
    return {
        "document_name": str(doc.GetDocumentName()),
        "document_path": str(doc.GetDocumentPath()),
        "fps": int(doc.GetFps()),
        "object_count": len(objects),
        "material_count": len(materials),
        "objects": objects,
        "materials": materials,
    }


def _load_document(c4d, path):
    flags = c4d.SCENEFILTER_OBJECTS | c4d.SCENEFILTER_MATERIALS
    doc = c4d.documents.LoadDocument(path, flags, None)
    if doc is None:
        raise RuntimeError("Cinema 4D could not load the document")
    return doc


def _kill_document(c4d, doc):
    if doc is not None:
        c4d.documents.KillDocument(doc)


def _save_document(c4d, doc, path, format_id=None):
    if format_id is None:
        format_id = c4d.FORMAT_C4DEXPORT
    flags = c4d.SAVEDOCUMENTFLAGS_DONTADDTORECENTLIST
    if not c4d.documents.SaveDocument(doc, path, flags, format_id):
        raise RuntimeError("Cinema 4D could not save the document")


def _find_unique_object(doc, name):
    matches = [obj for obj, _path, _depth in _walk_objects(doc) if obj.GetName() == name]
    if not matches:
        raise ValueError("Object does not exist: %s" % name)
    if len(matches) > 1:
        raise ValueError("Object name is ambiguous: %s" % name)
    return matches[0]


def _set_transform(c4d, obj, params):
    translation = _vector(params.get("translation", [0, 0, 0]), "translation")
    rotation = _vector(params.get("rotation_hpb_degrees", [0, 0, 0]), "rotation_hpb_degrees")
    scale = _vector(params.get("scale", [1, 1, 1]), "scale", positive=True)
    obj.SetAbsPos(_as_vector(c4d, translation))
    obj.SetAbsRot(_as_vector(c4d, [math.radians(item) for item in rotation]))
    obj.SetAbsScale(_as_vector(c4d, scale))


def _set_parameter(c4d, obj, constant_name, value):
    parameter_id = getattr(c4d, constant_name, None)
    if parameter_id is None:
        raise RuntimeError("Cinema 4D does not expose parameter %s" % constant_name)
    obj[parameter_id] = value


def system_status(_params):
    import c4d

    return {
        "cinema4d_version": int(c4d.GetC4DVersion()),
        "api_version": _version_string(c4d.GetAPIVersion()),
        "python_version": sys.version.split()[0],
        "headless": True,
    }


def document_create(params):
    import c4d

    doc = c4d.documents.BaseDocument()
    try:
        output_path = params["output_path"]
        _save_document(c4d, doc, output_path)
        return _document_payload(doc)
    finally:
        _kill_document(c4d, doc)


def document_inspect(params):
    import c4d

    doc = _load_document(c4d, params["document_path"])
    try:
        return _document_payload(doc)
    finally:
        _kill_document(c4d, doc)


def document_validate(params):
    import c4d

    doc = _load_document(c4d, params["document_path"])
    try:
        objects = list(_walk_objects(doc))
        names = {}
        for obj, _path, _depth in objects:
            names.setdefault(str(obj.GetName()), 0)
            names[str(obj.GetName())] += 1
        duplicate_names = sorted(name for name, count in names.items() if count > 1)
        return {
            "valid": True,
            "object_count": len(objects),
            "duplicate_object_names": duplicate_names,
            "warnings": ["duplicate_object_names"] if duplicate_names else [],
        }
    finally:
        _kill_document(c4d, doc)


def document_save_copy(params):
    import c4d

    doc = _load_document(c4d, params["document_path"])
    try:
        _save_document(c4d, doc, params["output_path"])
        return {"object_count": len(list(_walk_objects(doc)))}
    finally:
        _kill_document(c4d, doc)


_PRIMITIVE_IDS = {
    "cube": "Ocube",
    "sphere": "Osphere",
    "cylinder": "Ocylinder",
    "cone": "Ocone",
    "torus": "Otorus",
    "plane": "Oplane",
}


def _apply_dimensions(c4d, obj, primitive, dimensions):
    if primitive == "cube":
        size = _vector(dimensions.get("size"), "dimensions.size", positive=True)
        _set_parameter(c4d, obj, "PRIM_CUBE_LEN", _as_vector(c4d, size))
    elif primitive == "sphere":
        _set_parameter(c4d, obj, "PRIM_SPHERE_RAD", _positive(dimensions.get("radius"), "radius"))
    elif primitive == "cylinder":
        _set_parameter(
            c4d,
            obj,
            "PRIM_CYLINDER_RADIUS",
            _positive(dimensions.get("radius"), "radius"),
        )
        _set_parameter(
            c4d,
            obj,
            "PRIM_CYLINDER_HEIGHT",
            _positive(dimensions.get("height"), "height"),
        )
    elif primitive == "cone":
        _set_parameter(
            c4d,
            obj,
            "PRIM_CONE_BRAD",
            _positive(dimensions.get("bottom_radius"), "bottom_radius", allow_zero=True),
        )
        _set_parameter(
            c4d,
            obj,
            "PRIM_CONE_TRAD",
            _positive(dimensions.get("top_radius"), "top_radius", allow_zero=True),
        )
        _set_parameter(
            c4d,
            obj,
            "PRIM_CONE_HEIGHT",
            _positive(dimensions.get("height"), "height"),
        )
    elif primitive == "torus":
        _set_parameter(
            c4d,
            obj,
            "PRIM_TORUS_OUTERRAD",
            _positive(dimensions.get("ring_radius"), "ring_radius"),
        )
        _set_parameter(
            c4d,
            obj,
            "PRIM_TORUS_INNERRAD",
            _positive(dimensions.get("pipe_radius"), "pipe_radius"),
        )
    elif primitive == "plane":
        _set_parameter(
            c4d,
            obj,
            "PRIM_PLANE_WIDTH",
            _positive(dimensions.get("width"), "width"),
        )
        _set_parameter(
            c4d,
            obj,
            "PRIM_PLANE_HEIGHT",
            _positive(dimensions.get("height"), "height"),
        )


def model_add_primitive(params):
    import c4d

    doc = _load_document(c4d, params["document_path"])
    try:
        name = str(params["name"])
        if any(obj.GetName() == name for obj, _path, _depth in _walk_objects(doc)):
            raise ValueError("Object already exists: %s" % name)
        primitive = str(params["primitive"])
        type_id = getattr(c4d, _PRIMITIVE_IDS[primitive])
        obj = c4d.BaseObject(type_id)
        if obj is None:
            raise RuntimeError("Cinema 4D could not allocate the primitive")
        obj.SetName(name)
        _apply_dimensions(c4d, obj, primitive, params.get("dimensions", {}))
        _set_transform(c4d, obj, params)
        doc.InsertObject(obj)
        _save_document(c4d, doc, params["document_path"])
        return {"created": _object_payload(obj, name, 0)}
    finally:
        _kill_document(c4d, doc)


def model_transform_object(params):
    import c4d

    doc = _load_document(c4d, params["document_path"])
    try:
        obj = _find_unique_object(doc, params["object_name"])
        _set_transform(c4d, obj, params)
        _save_document(c4d, doc, params["document_path"])
        path = next(path for item, path, _depth in _walk_objects(doc) if item == obj)
        return {"updated": _object_payload(obj, path, path.count("/"))}
    finally:
        _kill_document(c4d, doc)


def model_remove_object(params):
    import c4d

    doc = _load_document(c4d, params["document_path"])
    try:
        obj = _find_unique_object(doc, params["object_name"])
        if obj.GetDown() is not None and not params.get("cascade"):
            raise ValueError("Object has children; set cascade=true to remove the subtree")
        removed = [path for item, path, _depth in _walk_objects(doc) if item == obj]
        if params.get("cascade"):
            prefix = removed[0] + "/"
            removed.extend(
                path for _item, path, _depth in _walk_objects(doc) if path.startswith(prefix)
            )
        obj.Remove()
        _save_document(c4d, doc, params["document_path"])
        return {"removed_paths": removed}
    finally:
        _kill_document(c4d, doc)


def model_import_geometry(params):
    import c4d

    doc = _load_document(c4d, params["document_path"])
    try:
        before_paths = {path for _obj, path, _depth in _walk_objects(doc)}
        flags = (
            c4d.SCENEFILTER_OBJECTS
            | c4d.SCENEFILTER_MATERIALS
            | c4d.SCENEFILTER_MERGESCENE
            | c4d.SCENEFILTER_NOUNDO
        )
        if not c4d.documents.MergeDocument(doc, params["input_path"], flags, None):
            suffix = os.path.splitext(params["input_path"])[1].lower()
            raise RuntimeError(
                "Cinema 4D runtime could not import %s; the required importer may not be available"
                % suffix
            )
        imported_paths = [
            path for _obj, path, _depth in _walk_objects(doc) if path not in before_paths
        ]
        if not imported_paths:
            raise RuntimeError("Cinema 4D import did not add any objects")
        _save_document(c4d, doc, params["document_path"])
        return {"input_path": params["input_path"], "imported_paths": imported_paths}
    finally:
        _kill_document(c4d, doc)


_EXPORT_FORMATS = {
    ".c4d": "FORMAT_C4DEXPORT",
    ".obj": "FORMAT_OBJ2EXPORT",
    ".fbx": "FORMAT_FBX_EXPORT",
    ".gltf": "FORMAT_GLTFEXPORT",
    ".glb": "FORMAT_GLTFEXPORT",
    ".stl": "FORMAT_STL_EXPORT",
    ".abc": "FORMAT_ABCEXPORT",
    ".dae": "FORMAT_DAE14EXPORT",
}


def document_export(params):
    import c4d

    doc = _load_document(c4d, params["document_path"])
    try:
        suffix = os.path.splitext(params["output_path"])[1].lower()
        constant_name = _EXPORT_FORMATS[suffix]
        format_id = getattr(c4d, constant_name, None)
        if format_id is None:
            raise RuntimeError("Cinema 4D does not support export format %s" % suffix)
        _save_document(c4d, doc, params["output_path"], format_id)
        return {"format": suffix, "object_count": len(list(_walk_objects(doc)))}
    finally:
        _kill_document(c4d, doc)


def document_render(params):
    import c4d

    doc = _load_document(c4d, params["document_path"])
    try:
        width = int(params["width"])
        height = int(params["height"])
        render_data = doc.GetActiveRenderData().GetDataInstance()
        render_data[c4d.RDATA_XRES] = float(width)
        render_data[c4d.RDATA_YRES] = float(height)
        bitmap = c4d.bitmaps.BaseBitmap()
        init_result = bitmap.Init(width, height, 24)
        if init_result != c4d.IMAGERESULT_OK:
            raise RuntimeError("Cinema 4D could not initialize the render bitmap")
        flags = c4d.RENDERFLAGS_EXTERNAL
        render_result = c4d.documents.RenderDocument(doc, render_data, bitmap, flags)
        if render_result != c4d.RENDERRESULT_OK:
            raise RuntimeError("Cinema 4D render failed with result %s" % render_result)
        suffix = os.path.splitext(params["output_path"])[1].lower()
        filter_id = c4d.FILTER_PNG if suffix == ".png" else c4d.FILTER_JPG
        save_result = bitmap.Save(params["output_path"], filter_id, c4d.BaseContainer())
        if save_result != c4d.IMAGERESULT_OK:
            raise RuntimeError("Cinema 4D could not save the rendered image")
        return {"width": width, "height": height, "format": suffix}
    finally:
        _kill_document(c4d, doc)


_METHODS = {
    "system.status": system_status,
    "document.create": document_create,
    "document.inspect": document_inspect,
    "document.validate": document_validate,
    "document.save_copy": document_save_copy,
    "document.export": document_export,
    "document.render": document_render,
    "model.add_primitive": model_add_primitive,
    "model.transform_object": model_transform_object,
    "model.remove_object": model_remove_object,
    "model.import_geometry": model_import_geometry,
}


def dispatch(method, params):
    handler = _METHODS.get(method)
    if handler is None:
        raise ValueError("Unknown Cinema 4D method: %s" % method)
    return handler(params)


def _write_json_atomic(path, payload):
    temporary = path + ".tmp"
    with open(temporary, "x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _wait_for_ack(path, timeout_secs=10.0):
    deadline = time.monotonic() + timeout_secs
    while not os.path.isfile(path):
        if time.monotonic() >= deadline:
            raise RuntimeError("parent acknowledgement timed out")
        time.sleep(0.01)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 5:
        raise SystemExit(
            "usage: cinema4d_driver.py REQUEST_JSON RESULT_JSON RUNTIME_JSON READY_ACK RESULT_ACK"
        )
    request_path, result_path, runtime_identity_path, ready_ack_path, result_ack_path = argv
    _write_json_atomic(runtime_identity_path, {"pid": os.getpid(), "protocol": 1})
    _wait_for_ack(ready_ack_path)
    try:
        with open(request_path, "r", encoding="utf-8") as stream:
            request = json.load(stream)
        result = dispatch(str(request.get("method", "")), request.get("params", {}))
        payload = {"ok": True, "result": result}
    except BaseException as error:
        payload = {
            "ok": False,
            "error": {
                "type": type(error).__name__,
            },
        }
    _write_json_atomic(result_path, payload)
    _wait_for_ack(result_ack_path)
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    exit_code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(exit_code)
