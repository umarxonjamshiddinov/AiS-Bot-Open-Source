from AI.models import Modality, ModelInfo


WIRED_INPUT_MODALITIES = frozenset({Modality.TEXT, Modality.IMAGE})


def is_input_wired(model: ModelInfo, modality: Modality) -> bool:
    return modality in WIRED_INPUT_MODALITIES and model.supports(modality)


def registered_but_not_wired(model: ModelInfo) -> frozenset[Modality]:
    return frozenset(model.input_modalities - WIRED_INPUT_MODALITIES)
