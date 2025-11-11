def test_strategy_model_and_task_names():
    from app.data.models import ttb_gmvmax

    assert hasattr(ttb_gmvmax, "TTBGmvMaxStrategyConfig")

    from app.tasks import ttb_gmvmax_tasks as tasks

    assert hasattr(tasks, "task_gmvmax_evaluate_strategy")
    assert tasks.task_gmvmax_evaluate_strategy.name == "gmvmax.evaluate_strategy"
