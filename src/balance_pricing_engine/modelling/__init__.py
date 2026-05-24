"""Trained models in this package are diagnostic only and are NOT consumed by the optimiser.

See docs/model_governance.md.
"""

from balance_pricing_engine.modelling.agentic import (
    ElasticityBaselineModel,
    ElasticityResponseModel,
    HistGBMResponseModel,
    LightGBMResponseModel,
    LogisticResponseModel,
    ModelRecord,
    ModelRegistry,
    ModelScoringAgent,
    ModelTrainingAgent,
    SurvivalResponseModel,
    TrainingConfig,
    TrainingResult,
    XGBoostResponseModel,
)
from balance_pricing_engine.modelling.datasets import (
    DEPOSIT_FEATURE_COLUMNS,
    DEPOSIT_TARGET_COLUMN,
    LOAN_FEATURE_COLUMNS,
    LOAN_TARGET_COLUMN,
    build_deposit_response_dataset,
    build_loan_response_dataset,
)
from balance_pricing_engine.modelling.models import (
    build_hist_gradient_boosting_model,
    build_logistic_regression_model,
    fit_response_model,
    predict_response_probability,
)
from balance_pricing_engine.modelling.persistence import load_model, save_model
from balance_pricing_engine.synthetic.customers import (
    generate_deposit_customers,
    generate_loan_customers,
)
from balance_pricing_engine.utils.random import make_rng

__all__ = [
    "ElasticityBaselineModel",
    "ElasticityResponseModel",
    "HistGBMResponseModel",
    "LightGBMResponseModel",
    "LogisticResponseModel",
    "SurvivalResponseModel",
    "XGBoostResponseModel",
    "ModelRecord",
    "ModelRegistry",
    "ModelScoringAgent",
    "ModelTrainingAgent",
    "TrainingConfig",
    "TrainingResult",
    "DEPOSIT_FEATURE_COLUMNS",
    "DEPOSIT_TARGET_COLUMN",
    "LOAN_FEATURE_COLUMNS",
    "LOAN_TARGET_COLUMN",
    "build_deposit_response_dataset",
    "build_loan_response_dataset",
    "build_hist_gradient_boosting_model",
    "build_logistic_regression_model",
    "fit_response_model",
    "predict_response_probability",
    "load_model",
    "save_model",
    "generate_deposit_customers",
    "generate_loan_customers",
    "make_rng",
]
