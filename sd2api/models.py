from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class TextContent(BaseModel):
    type: Literal["text"]
    text: str = Field(min_length=1, max_length=32000)


class ImageURLValue(BaseModel):
    url: str


class ImageURLContent(BaseModel):
    type: Literal["image_url"]
    image_url: ImageURLValue | str
    role: Literal["first_frame", "last_frame", "reference_image"] | None = None


class VideoURLContent(BaseModel):
    type: Literal["video_url"]
    video_url: ImageURLValue | str
    role: Literal["reference_video"] = "reference_video"


class AudioURLContent(BaseModel):
    type: Literal["audio_url"]
    audio_url: ImageURLValue | str
    role: Literal["reference_audio"] = "reference_audio"


SeedanceMediaContent = ImageURLContent | VideoURLContent | AudioURLContent
SeedanceContent = TextContent | SeedanceMediaContent


class SeedanceCreateRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str = "seedance-2.0"
    content: list[SeedanceContent]
    duration: int = Field(default=5, ge=4, le=15)
    ratio: str = "adaptive"
    resolution: str = "720p"
    seed: int | None = None
    camera_fixed: bool = False
    watermark: bool = False
    generate_audio: bool = False

    @model_validator(mode="after")
    def validate_content(self) -> "SeedanceCreateRequest":
        images = sum(isinstance(item, ImageURLContent) for item in self.content)
        videos = sum(isinstance(item, VideoURLContent) for item in self.content)
        audios = sum(isinstance(item, AudioURLContent) for item in self.content)
        has_text = any(
            isinstance(item, TextContent) and item.text.strip() for item in self.content
        )
        if not has_text and not (images or videos):
            raise ValueError("content must include text, an image, or a video")
        if images > 9:
            raise ValueError("multimodal reference supports at most 9 images")
        if videos > 3:
            raise ValueError("multimodal reference supports at most 3 videos")
        if audios > 3:
            raise ValueError("multimodal reference supports at most 3 audio clips")
        if audios and not (images or videos):
            raise ValueError("audio references require at least one image or video")
        frame_images = [
            item
            for item in self.content
            if isinstance(item, ImageURLContent)
            and item.role in {"first_frame", "last_frame"}
        ]
        if frame_images:
            roles = [item.role for item in frame_images]
            valid_single = len(self.media) == 1 and roles == ["first_frame"]
            valid_pair = (
                len(self.media) == 2
                and len(frame_images) == 2
                and set(roles) == {"first_frame", "last_frame"}
            )
            if not (valid_single or valid_pair):
                raise ValueError(
                    "first_frame/last_frame cannot be mixed with reference media; "
                    "use role=reference_image for Reference to video"
                )
        return self

    @property
    def prompt(self) -> str:
        return "\n".join(
            item.text.strip()
            for item in self.content
            if isinstance(item, TextContent) and item.text.strip()
        )

    @property
    def image_urls(self) -> list[str]:
        values: list[str] = []
        for item in self.content:
            if isinstance(item, ImageURLContent):
                values.append(
                    item.image_url if isinstance(item.image_url, str) else item.image_url.url
                )
        return values

    @property
    def media(self) -> list[tuple[Literal["image", "video", "audio"], str, str | None]]:
        values: list[tuple[Literal["image", "video", "audio"], str, str | None]] = []
        for item in self.content:
            if isinstance(item, ImageURLContent):
                url = item.image_url if isinstance(item.image_url, str) else item.image_url.url
                values.append(("image", url, item.role))
            elif isinstance(item, VideoURLContent):
                url = item.video_url if isinstance(item.video_url, str) else item.video_url.url
                values.append(("video", url, item.role))
            elif isinstance(item, AudioURLContent):
                url = item.audio_url if isinstance(item.audio_url, str) else item.audio_url.url
                values.append(("audio", url, item.role))
        return values

    @property
    def generation_mode(self) -> Literal["text", "image", "reference", "first_last"]:
        media = self.media
        if not media:
            return "text"
        images = [item for item in self.content if isinstance(item, ImageURLContent)]
        if len(media) == 1 and len(images) == 1 and images[0].role in {None, "first_frame"}:
            return "image"
        if (
            len(media) == 2
            and len(images) == 2
            and {item.role for item in images} == {"first_frame", "last_frame"}
        ):
            return "first_last"
        return "reference"


class OpenAIImageReference(BaseModel):
    image_url: str | None = None
    file_id: str | None = None

    @model_validator(mode="after")
    def exactly_one_source(self) -> "OpenAIImageReference":
        if (self.image_url is None) == (self.file_id is None):
            raise ValueError("input_reference requires exactly one of image_url or file_id")
        return self


class OpenAICreateVideoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=32000)
    model: str = "sora-2"
    seconds: int | str = 4
    size: Literal["720x1280", "1280x720", "1024x1792", "1792x1024"] = "720x1280"
    input_reference: OpenAIImageReference | None = None
    references: list[SeedanceMediaContent] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_seconds(self) -> "OpenAICreateVideoRequest":
        try:
            seconds = int(self.seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError("seconds must be an integer from 4 through 15") from exc
        if not 4 <= seconds <= 15:
            raise ValueError("seconds must be from 4 through 15")
        self.seconds = seconds
        if self.input_reference is not None and self.references:
            raise ValueError("input_reference and references are mutually exclusive")
        images = sum(isinstance(item, ImageURLContent) for item in self.references)
        videos = sum(isinstance(item, VideoURLContent) for item in self.references)
        audios = sum(isinstance(item, AudioURLContent) for item in self.references)
        if images > 9 or videos > 3 or audios > 3:
            raise ValueError("reference limits are 9 images, 3 videos, and 3 audio clips")
        if audios and not (images or videos):
            raise ValueError("audio references require at least one image or video")
        return self


class UpstreamTask(BaseModel):
    id: str
    status: Literal["queued", "running", "succeeded", "failed"]
    progress: int = Field(default=0, ge=0, le=100)
    video_id: str | None = None
    video_url: str | None = None
    poster_url: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class AccountCreateRequest(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    name: str = Field(min_length=1, max_length=128)
    username: str | None = Field(default=None, min_length=1, max_length=320)
    password: SecretStr | None = None
    email_address: str | None = Field(default=None, min_length=3, max_length=320)
    auto_login: bool = True
    start: bool = True

    @model_validator(mode="after")
    def credentials_are_complete(self) -> "AccountCreateRequest":
        if (self.username is None) != (self.password is None):
            raise ValueError("username and password must be supplied together")
        return self


class AccountUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    enabled: bool | None = None
    username: str | None = Field(default=None, min_length=1, max_length=320)
    password: SecretStr | None = None
    email_address: str | None = Field(default=None, min_length=3, max_length=320)
    auto_login: bool | None = None


class AccountLoginRequest(BaseModel):
    wait: bool = False
