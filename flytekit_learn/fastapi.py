"""Utilities for the FastAPI integration."""

from enum import Enum
from functools import wraps
from inspect import signature
from typing import Any, Dict, List, Optional

from fastapi import Body, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, create_model

from flytekit.models import filters
from flytekit.models.admin.common import Sort
from flytekit.remote import FlyteWorkflowExecution


class Endpoints(Enum):
    TRAINER = 1
    PREDICTOR = 2
    EVALUATOR = 3
    LABELLER = 4


APP_LABELLER_CACHE = {}


def app_wrapper(model, app):
    @app.get("/", response_class=HTMLResponse)
    def root():
        return """
            <html>
                <head>
                    <title>flytekit-learn</title>
                </head>
                <body>
                    <h1>flytekit-learn</h1>
                    <p>The easiest way to build and deploy models</p>
                </body>
            </html>
        """
    
    app.get = _app_method_wrapper(app.get, model)
    app.post = _app_method_wrapper(app.post, model)
    app.put = _app_method_wrapper(app.put, model)


def _app_method_wrapper(app_method, model):

    @wraps(app_method)
    def wrapper(*args, **kwargs):
        return _app_decorator_wrapper(app_method(*args, **kwargs), model, app_method)

    return wrapper


def _app_decorator_wrapper(decorator, model, app_method):

    @wraps(decorator)
    def wrapper(fn):

        if app_method.__name__ not in {"get", "post", "put"}:
            raise ValueError(f"flytekit-learn only supports 'get' and 'post' methods: found {app_method.__name__}")

        def _train_endpoint(
            local: bool = False,
            model_name: Optional[str] = None,
            inputs: Dict = Body(...),
        ):
            if issubclass(type(inputs), BaseModel):
                inputs = inputs.dict()

            if not local:
                # TODO: make the model name a property of the Model object
                train_wf = model._remote.fetch_workflow(
                    name=f"{model_name}.train" if model_name else model.train_workflow_name
                )
                execution = model._remote.execute(train_wf, inputs=inputs, wait=True)
                trained_model = execution.outputs["trained_model"]
                metrics = execution.outputs["metrics"]
            else:
                trained_model, metrics = model.train(**inputs)
                model._latest_model = trained_model
                model._metrics = metrics
            return {
                "trained_model": str(trained_model),
                "metrics": metrics,
            }

        def _predict_endpoint(
            local: bool = False,
            model_name: Optional[str] = None,
            model_version: str = "latest",
            model_source: str = "remote",
            inputs: Optional[Dict] = Body(None),
            features: Optional[List[Dict[str, Any]]] = Body(None)
        ):
            version = None if model_version == "latest" else model_version
            if model_source == "remote":
                train_wf = model._remote.fetch_workflow(
                    name=f"{model_name}.train" if model_name else model.train_workflow_name,
                    version=version,
                )
                [latest_training_execution, *_], _ = model._remote.client.list_executions_paginated(
                    train_wf.id.project,
                    train_wf.id.domain,
                    limit=1,
                    filters=[
                        filters.Equal("launch_plan.name", train_wf.id.name),
                        filters.Equal("phase", "SUCCEEDED"),
                    ],
                    sort_by=Sort("created_at", Sort.Direction.DESCENDING),
                )
                latest_training_execution = FlyteWorkflowExecution.promote_from_model(
                    latest_training_execution
                )
                model._remote.sync(latest_training_execution)
                latest_model = latest_training_execution.outputs["trained_model"]
            else:
                if model._latest_model is None:
                    raise HTTPException(status_code=500, detail="trained model not found")
                latest_model = model._latest_model

            workflow_inputs = {"model": latest_model}
            if inputs:
                workflow_inputs.update(inputs)
                predict_wf = model._remote.fetch_workflow(
                    name=f"{model_name}.predict_workflow_name" if model_name else model.predict_workflow_name,
                    version=version,
                )
            elif features:
                features = model._dataset.get_features(features)
                workflow_inputs["features"] = features
                predict_wf = model._remote.fetch_workflow(
                    name=(
                        f"{model_name}.predict_from_features_workflow_name"
                        if model_name
                        else model.predict_from_features_workflow_name
                    ),
                    version=version,
                )

            if not local:
                predictions = model._remote.execute(predict_wf, inputs=workflow_inputs, wait=True).outputs["o0"]
            else:
                predictions = model.predict(**workflow_inputs)
            return predictions

        def _labeller_endpoint(
            session_id: str,
            batch_size: int = 3,
            submit: bool = False,
            reader_inputs: Optional[Dict] = Body(None),
            submission: Optional[List[Dict[str, Any]]] = Body(None),
        ):
            if session_id not in APP_LABELLER_CACHE:
                generator = model._dataset._labeller(session_id, batch_size, **reader_inputs)
                awaiting_submission = False
                APP_LABELLER_CACHE[session_id] = {
                    "generator": generator,
                    "awaiting_submission": awaiting_submission,
                    "session_complete": False,
                }
            else:
                generator = APP_LABELLER_CACHE[session_id]["generator"]
                awaiting_submission = APP_LABELLER_CACHE[session_id]["awaiting_submission"]

            if submit:
                if not awaiting_submission:
                    raise HTTPException(status_code=400, detail="Labels already submitted for current batch")
                generator.send(submission)
                APP_LABELLER_CACHE[session_id]["awaiting_submission"] = False
                return {
                    "success": True,
                    "session_complete": False,
                }

            if awaiting_submission:
                raise HTTPException(status_code=400, detail="Awaiting labels from current batch")

            try:
                batch = next(generator)
                session_complete = False
                APP_LABELLER_CACHE[session_id]["awaiting_submission"] = True
            except StopIteration:
                batch = None
                session_complete = True
                APP_LABELLER_CACHE[session_id]["session_complete"] = True

            return {
                "batch": batch,
                "session_complete": session_complete,
            }

        endpoint_fn = {
            Endpoints.PREDICTOR: _predict_endpoint,
            Endpoints.TRAINER: _train_endpoint,
            Endpoints.LABELLER: _labeller_endpoint,
        }[fn.__app_method__]

        if endpoint_fn is _train_endpoint and model._hyperparameters:
            HyperparameterModel = create_model(
                "HyperparameterModel", **{k: (v, ...) for k, v in model._hyperparameters.items()}
            )
            sig = signature(_train_endpoint)
            _train_endpoint.__signature__ = sig.replace(parameters=[
                sig.parameters[p].replace(annotation=HyperparameterModel)
                if p == "hyperparameters"
                else sig.parameters[p]
                for p in sig.parameters
            ])

        decorator(endpoint_fn)
        return fn

    return wrapper
