import os
from celery import Celery
from celery.schedules import crontab


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
    # Optional periodic schedules (enable if running celery beat)
    if os.getenv("ENABLE_CELERY_BEAT", "true").lower() in ("1","true","yes"):
        app.conf.timezone = os.getenv("TZ", "UTC")
        app.conf.beat_schedule = {
            # Process queued items more frequently for higher throughput
            "queue-consumer-once": {
                "task": "app.tasks.queue_consumer_once",
                "schedule": 0.2,  # seconds - 5x faster processing
            },
            # Run ongoing scheduler hourly
            "schedule-ongoing-hourly": {
                "task": "app.tasks.schedule_ongoing",
                "schedule": crontab(minute=0),
            },
            # Inflate sigma daily at 00:00 UTC
            "inflate-sigma-daily": {
                "task": "app.tasks.inflate_sigma_for_inactive",
                "schedule": crontab(minute=0, hour=0),
            },
        }
    return app


celery_app = _make_celery_app() 