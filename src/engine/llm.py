"""Google Gemini LLM client and structured output schemas for JobApplier-Core."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final, Literal, TypeVar

from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, Field, ValidationError

GEMINI_PRO: Final[str] = "gemini-1.5-pro"
GEMINI_FLASH: Final[str] = "gemini-1.5-flash"
SupportedModel = Literal["gemini-1.5-pro", "gemini-1.5-flash"]
SUPPORTED_MODELS: Final[tuple[str, ...]] = (GEMINI_PRO, GEMINI_FLASH)

PLACEHOLDER_API_KEY: Final[str] = "your_gemini_api_key_here"
GEMINI_API_KEY_ENV: Final[str] = "GEMINI_API_KEY"

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class MissingGeminiCredentialsError(RuntimeError):
    """Raised when GEMINI_API_KEY is missing, blank, or still set to the placeholder."""


class GeminiResponseError(RuntimeError):
    """Raised when the Gemini API returns an empty or invalid structured payload."""


class JobDescriptionSchema(BaseModel):
    """Structured representation of a parsed job posting."""

    title: str = Field(..., description="Normalized job title from the posting.")
    company: str = Field(..., description="Hiring company or organization name.")
    skills_required: list[str] = Field(
        default_factory=list,
        description="Technical and domain skills explicitly requested in the posting.",
    )
    experience_level: str = Field(
        ...,
        description="Expected seniority or experience level (e.g., Intern, Junior, Mid, Senior).",
    )
    custom_screening_questions: list[str] = Field(
        default_factory=list,
        description="Application screening questions extracted from the job posting.",
    )


class FormFieldMappingSchema(BaseModel):
    """Structured mapping between a detected application form field and a candidate value."""

    field_id: str = Field(..., description="DOM id, name, or selector identifier of the form field.")
    target_label: str = Field(..., description="Human-readable label associated with the form field.")
    value: str = Field(..., description="Resolved value to enter or select for the field.")
    field_type: str = Field(
        ...,
        description="Control type of the field (e.g., text, textarea, select, radio, checkbox, file).",
    )


def validate_model(model: str, *, allow_unlisted: bool = False) -> str:
    """Validate that a Gemini model identifier is supported.

    Args:
        model: Model identifier string to validate.
        allow_unlisted: When True, permits non-standard Gemini model names as long
            as the value is a non-empty string.

    Returns:
        Normalized model identifier string.

    Raises:
        ValueError: If the model name is blank or not in ``SUPPORTED_MODELS``.
    """
    if not isinstance(model, str) or not model.strip():
        raise ValueError("Model name must be a non-empty string.")

    normalized = model.strip()
    if not allow_unlisted and normalized not in SUPPORTED_MODELS:
        allowed = ", ".join(SUPPORTED_MODELS)
        raise ValueError(f"Unsupported Gemini model '{normalized}'. Expected one of: {allowed}.")
    return normalized


def load_gemini_api_key(
    env_file: str | Path | None = None,
    *,
    api_key: str | None = None,
) -> str:
    """Load and validate the Gemini API key from environment or ``.env``.

    Args:
        env_file: Optional path to a ``.env`` file. When ``None``, ``python-dotenv``
            discovers ``.env`` automatically from the current working tree.
        api_key: Optional explicit API key override.

    Returns:
        Validated non-empty API key string.

    Raises:
        MissingGeminiCredentialsError: If ``GEMINI_API_KEY`` is unset, empty, or
            still equals ``your_gemini_api_key_here``.
    """
    if api_key is None:
        load_dotenv(dotenv_path=env_file, override=False)
        raw_key = os.getenv(GEMINI_API_KEY_ENV, "")
    else:
        raw_key = api_key

    cleaned_key = raw_key.strip()
    if not cleaned_key or cleaned_key == PLACEHOLDER_API_KEY:
        raise MissingGeminiCredentialsError(
            "GEMINI_API_KEY is missing, empty, or still set to the placeholder value "
            f"'{PLACEHOLDER_API_KEY}'. Configure a valid key in .env or the environment."
        )
    return cleaned_key


class GeminiClient:
    """Typed wrapper around the official ``google-genai`` SDK with Pydantic v2 outputs."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        default_model: str = GEMINI_FLASH,
        env_file: str | Path | None = None,
        allow_unlisted_models: bool = False,
        client: genai.Client | None = None,
    ) -> None:
        """Initialize a Gemini client and validate credentials and default model.

        Args:
            api_key: Optional explicit API key. If omitted, loaded via ``python-dotenv``.
            default_model: Default Gemini model to use (``gemini-1.5-flash`` by default).
            env_file: Optional explicit ``.env`` file path for credential loading.
            allow_unlisted_models: Allow model names outside ``SUPPORTED_MODELS``.
            client: Optional pre-built ``genai.Client`` instance (primarily for testing).
        """
        self.allow_unlisted_models = allow_unlisted_models
        self.default_model = validate_model(default_model, allow_unlisted=allow_unlisted_models)

        if client is not None:
            self.client = client
        else:
            resolved_key = load_gemini_api_key(env_file=env_file, api_key=api_key)
            self.client = genai.Client(api_key=resolved_key)

    def generate_structured(
        self,
        prompt: str,
        response_schema: type[SchemaT],
        *,
        model: str | None = None,
        system_instruction: str | None = None,
        temperature: float | None = 0.1,
    ) -> SchemaT:
        """Generate a JSON response constrained to ``response_schema`` and validate it.

        Args:
            prompt: User prompt sent to Gemini.
            response_schema: Pydantic v2 ``BaseModel`` subclass defining the output schema.
            model: Optional model override; defaults to ``self.default_model``.
            system_instruction: Optional system instruction string.
            temperature: Sampling temperature passed to ``GenerateContentConfig``.

        Returns:
            Validated instance of ``response_schema``.

        Raises:
            ValueError: If ``prompt`` is empty or ``model`` is unsupported.
            GeminiResponseError: If the API response is empty or fails schema validation.
        """
        if not prompt or not prompt.strip():
            raise ValueError("Prompt must be a non-empty string.")

        target_model = validate_model(
            model if model is not None else self.default_model,
            allow_unlisted=self.allow_unlisted_models,
        )

        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=response_schema,
            system_instruction=system_instruction,
            temperature=temperature,
        )

        response = self.client.models.generate_content(
            model=target_model,
            contents=prompt,
            config=config,
        )

        parsed_payload = getattr(response, "parsed", None)
        if isinstance(parsed_payload, response_schema):
            return parsed_payload
        if isinstance(parsed_payload, dict):
            try:
                return response_schema.model_validate(parsed_payload)
            except ValidationError as exc:
                raise GeminiResponseError(
                    f"Gemini parsed payload failed {response_schema.__name__} validation: {exc}"
                ) from exc

        raw_text = getattr(response, "text", None)
        if not isinstance(raw_text, str) or not raw_text.strip():
            raise GeminiResponseError("Gemini returned an empty response for structured output.")

        try:
            return response_schema.model_validate_json(raw_text)
        except ValidationError as exc:
            raise GeminiResponseError(
                f"Gemini JSON output failed {response_schema.__name__} validation: {exc}"
            ) from exc

    def parse_job_description(
        self,
        job_description_text: str,
        *,
        model: str | None = None,
    ) -> JobDescriptionSchema:
        """Extract structured job requirements and screening questions from raw JD text."""
        if not job_description_text or not job_description_text.strip():
            raise ValueError("job_description_text must be a non-empty string.")

        system_instruction = (
            "You are a precise job-description parser. Extract only facts present in the "
            "posting without inventing skills or requirements."
        )
        prompt = (
            "Parse the following job posting into the requested JSON schema.\n\n"
            f"JOB POSTING:\n{job_description_text.strip()}"
        )
        return self.generate_structured(
            prompt,
            JobDescriptionSchema,
            model=model,
            system_instruction=system_instruction,
        )

    def map_form_field(
        self,
        field_context: str,
        candidate_context: str,
        *,
        model: str | None = None,
    ) -> FormFieldMappingSchema:
        """Map an application form field to a truthful candidate profile value."""
        if not field_context or not field_context.strip():
            raise ValueError("field_context must be a non-empty string.")
        if not candidate_context or not candidate_context.strip():
            raise ValueError("candidate_context must be a non-empty string.")

        system_instruction = (
            "You map job-application form fields strictly using provided candidate data. "
            "Never fabricate credentials, dates, or contact details."
        )
        prompt = (
            "Determine the best field value for the form field below using the candidate context.\n\n"
            f"FORM FIELD:\n{field_context.strip()}\n\n"
            f"CANDIDATE CONTEXT:\n{candidate_context.strip()}"
        )
        return self.generate_structured(
            prompt,
            FormFieldMappingSchema,
            model=model,
            system_instruction=system_instruction,
        )


__all__ = [
    "GEMINI_API_KEY_ENV",
    "GEMINI_FLASH",
    "GEMINI_PRO",
    "PLACEHOLDER_API_KEY",
    "SUPPORTED_MODELS",
    "FormFieldMappingSchema",
    "GeminiClient",
    "GeminiResponseError",
    "JobDescriptionSchema",
    "MissingGeminiCredentialsError",
    "SupportedModel",
    "load_gemini_api_key",
    "validate_model",
]
