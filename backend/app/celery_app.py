import os
from celery import Celery


def _make_celery_app() -> Celery:
    broker_url = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
    result_backend = os.getenv("CELERY_RESULT_BACKEND", broker_url)

    app = Celery("gridbot_brawl", broker=broker_url, backend=result_backend, include=["app.tasks"]) 

    # Route tasks to specific queues
    app.conf.task_queues = {
        # default queue still exists implicitly
    }
    app.conf.task_routes = {
        # Route task names to queues
        "app.tasks.run_match": {"queue": "simulation"},
        "app.tasks.schedule_evaluation_for_team": {"queue": "simulation"},
        "app.tasks.run_baseline_test": {"queue": "baseline"},
    }
    app.conf.task_default_queue = "simulation"
    app.conf.worker_hijack_root_logger = False
    return app


celery_app = _make_celery_app() 