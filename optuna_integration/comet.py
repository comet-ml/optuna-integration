import functools
from typing import Any
from typing import Callable
from typing import Dict
from typing import Optional
from typing import Sequence
from typing import Union
import json

import optuna
from optuna.study.study import ObjectiveFuncType
from optuna_integration._imports import try_import

with try_import() as _imports:
    import comet_ml


class CometCallback:
    """
    A callback for logging Optuna study trials to a Comet ML Experiment. Comet ML must be installed to run.

    This callback is intended for use with Optuna's study.optimize() method. It ensures that all trials
    from an Optuna study are logged to a single Comet Experiment, facilitating organized tracking 
    of hyperparameter optimization. The callback supports both single and multi-objective optimization.

    In a distributed training context, where trials from the same study might occur on different machines,
    this callback ensures consistency by logging to the same Comet Experiment using an experiment key 
    stored within the study's user attributes.

    By default, Trials are logged as Comet Experiments, which will automatically log code, system metrics,
    and many other values. However, it also adds some computational overhead (potentially a few seconds).

    Parameters:
    - study (optuna.study.Study): The Optuna study object to which the callback is attached.
    - workspace (str) - Optional: The workspace in Comet ML where the project resides.
    - project_name (str) - Optional: The name of the project in Comet ML where the experiment will be logged. Defaults to "general"
    - metric_names ([str]) - Optional: A list of the names of your objective metrics.

    Usage:
    ```
    study = optuna.create_study(directions=["minimize", "maximize"])
    comet_callback = CometCallback(study, metric_names=["accuracy", "top_k_accuracy"],
                                     project_name="your_project_name", workspace="your_workspace")
    study.optimize(your_objective_function, n_trials=100, callbacks=[comet_callback])
    ```

    Note:
    The callback checks for an existing Comet Experiment key in the study's user attributes. If present, it initializes
    an ExistingExperiment; otherwise, it creates a new APIExperiment and stores its key in the study for future reference.

    You will need a Comet API key to log data to Comet.

    You can also log extra data directly to your Trial's Experiment via the objective function by using
    the @CometCallback.track_in_comet decorator, which exposes an `experiment` property on your trial, like so:
    ```
    study = optuna.create_study(directions=["minimize", "maximize"])
    comet_callback = CometCallback(study, metric_names=["accuracy", "top_k_accuracy"],
                                     project_name="your_project_name", workspace="your_workspace")
    
    @comet_callback.track_in_comet()
    def your_objective(trial):
        trial.experiment.log_other("foo", "bar")
        # Rest of your objective function...

    study.optimize(your_objective, n_trials=100, callbacks=[comet_callback])
    """

    def __init__(
        self, 
        study: optuna.study.Study, 
        workspace: Optional[str] = None,
        project_name: Optional[str] = 'general', 
        metric_names: Optional[Sequence[str]] = None
    ) -> None:
        self._project_name = project_name
        self._workspace = workspace
        self._study = study
        
        if metric_names is None:
            metric_names = []
            
        self._metric_names = metric_names

        if self._workspace is None:
            API = comet_ml.api.API()
            self._workspace = API.get_default_workspace()

        # APIExperiment associated with the Optuna Study
        self.study_experiment = self._init_optuna_study_experiment(study)
        
        # Log the directions of the objectives
        for i, direction in enumerate(study.directions):
            direction_str = "minimize" if direction == optuna.study.StudyDirection.MINIMIZE else "maximize"
            metric_name = metric_names[i] if i < len(metric_names) else i
            self.study_experiment.log_other(f"direction_of_objective_{metric_name}", direction_str)

        # Dictionary of experiment keys associated with specific Optuna Trials
        self._trial_experiments = study.user_attrs.get('trial_experiments')
        if self._trial_experiments is None:
            self._trial_experiments = {}
            study.user_attrs

    def __call__(
        self, 
        study: optuna.Study, 
        trial: optuna.Trial
    ) -> None:
        trial_experiment = self._init_optuna_trial_experiment(study, trial)

        trial_experiment.log_parameters(trial.params)
        trial_experiment.log_other('trial_number', trial.number)
        trial_experiment.add_tag(f"trial_number_{trial.number}")

        # Check if the study is multi-objective
        if trial.values and len(trial.values) > 1:
            # Log each objective value separately for multi-objective optimization
            for i, val in enumerate(trial.values):
                metric_name = self._metric_names[i] if i < len(self._metric_names) else i
                trial_experiment.log_metric(f"{metric_name}", val)
        else:
            # Log single objective value
            metric_name = self._metric_names[0] if len(self._metric_names) > 0 else "objective_value"
            trial_experiment.log_optimization(
                optimization_id=study.study_name,
                metric_name=metric_name, 
                metric_value=trial.value,
                parameters=trial.params,
                objective=study.direction
            )

        # Log the best trials to the APIExperiment associated with the Study.
        self.study_experiment.log_other("best_trials", json.dumps([ trial.number for trial in study.best_trials ]))

        trial_experiment.end()

    def _init_optuna_study_experiment(
        self, 
        study: optuna.Study
    ) -> comet_ml.APIExperiment:
        # Check if we've already created an APIExperiment for this Study 
        experiment_key = study.user_attrs.get("comet_study_experiment_key")

        # Load the existing APIExperiment, if present. Else, make a new APIExperiment
        if experiment_key:
            study_experiment = comet_ml.APIExperiment(previous_experiment=experiment_key)
        else:
            study_experiment = comet_ml.APIExperiment(workspace=self._workspace, project_name=self._project_name)
            study_experiment.add_tag("optuna_study")
            study_experiment.log_other("optuna_study_name", study.study_name)
            study_experiment.log_other("optuna_storage", type(study._storage).__name__)
            study.set_user_attr("comet_study_experiment_key", study_experiment.key)

        return study_experiment

    def _init_optuna_trial_experiment(
        self,
        study: optuna.Study,
        trial: optuna.Trial
    ) -> comet_ml.Experiment:

        # Check to see if the Trial experiment already exists
        experiment_key = self._trial_experiments.get(trial.number)

        # Check to see if there is a current active experiment in this environment
        if hasattr(comet_ml, "active_experiment"):
            if experiment_key == comet_ml.active_experiment.get_key():
                # No need to re-initialize if we already have the right experiment
                return comet_ml.active_experiment
            elif comet_ml.active_experiment.ended is False:
                comet_ml.active_experiment.end()

        # Load the existing Experiment, if present. Else, make a new Experiment
        if experiment_key:
            experiment = comet_ml.ExistingExperiment(previous_experiment=experiment_key)
        else:
            experiment = comet_ml.Experiment(workspace=self._workspace, project_name=self._project_name)

            experiment.add_tag("optuna_trial")
            experiment.log_other("optuna_study_name", study.study_name)

            self._trial_experiments[trial.number] = experiment.get_key()

        setattr(comet_ml, "active_experiment", experiment)
        return experiment

    def track_in_comet(self) -> Callable:
        def decorator(func: ObjectiveFuncType) -> ObjectiveFuncType:
            @functools.wraps(func)
            def wrapper(trial: optuna.trial.Trial) -> Union[float, Sequence[float]]:
                experiment = self._init_optuna_trial_experiment(self._study, trial)
                trial.experiment = experiment
                return func(trial)

            return wrapper

        return decorator

                

        