#!/bin/env python3
# -*- coding: utf-8 -*-
# vim: ts=4 sw=4 et

from celery.result import AsyncResult
from flask import Blueprint
from flask import request
from flask import jsonify

from task_app.checks.mapstore import check_res, check_all_mapstore_res, check_all_mapstore_res_subtasks

tasks_bp = Blueprint("tasks", __name__, url_prefix="/tasks")

@tasks_bp.get("/result/<id>")
def result(id: str) -> dict[str, object]:
    result = AsyncResult(id)
    ready = result.ready()
    # date_done is a datetime
    finished = result.date_done
    return {
        "ready": ready,
        "task": result.name,
        "finished": (finished.strftime('%s') if finished is not None else False),
        "args": result.args,
        "successful": result.successful() if ready else None,
        "value": result.get() if ready else result.result,
    }

@tasks_bp.route("/check/map/<int:mapid>.json")
def check_map(mapid):
    result = check_res.delay('MAP',mapid)
    return {"result_id": result.id}

@tasks_bp.route("/check/context/<int:ctxid>.json")
def check_ctx(ctxid):
    result = check_res.delay('CONTEXT',ctxid)
    return {"result_id": result.id}

@tasks_bp.route("/check/mapstore", methods=['POST'])
def check_mapstore():
    as_subtasks = request.form.get("subtasks", default=0, type=int)
    if as_subtasks != 0:
        result = check_all_mapstore_res_subtasks.delay()
    else:
        result = check_all_mapstore_res.delay()
    return {"result_id": result.id}
