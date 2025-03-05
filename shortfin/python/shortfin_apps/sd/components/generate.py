# Copyright 2024 Advanced Micro Devices, Inc.
#
# Licensed under the Apache License v2.0 with LLVM Exceptions.
# See https://llvm.org/LICENSE.txt for license information.
# SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception

import asyncio
import logging

from datetime import datetime

from dataclasses import dataclass

from typing import (
    Any,
    Protocol,
    TypeVar,
    Union,
)

from fastapi.responses import JSONResponse

from shortfin_apps.types.Base64CharacterEncodedByteSequence import (
    Base64CharacterEncodedByteSequence,
)

from shortfin_apps.utilities.image import png_from
from shortfin_apps.text_to_image.TextToImageInferenceOutput import (
    TextToImageInferenceOutput,
)

import shortfin as sf

# TODO: Have a generic "Responder" interface vs just the concrete impl.
from shortfin.interop.fastapi import FastAPIResponder

from .io_struct import GenerateReqInput
from .messages import SDXLInferenceExecRequest
from .service import SDXLGenerateService
from .metrics import measure

logger = logging.getLogger("shortfin-sd.generate")


class GenerateImageProcess(sf.Process):
    """Process instantiated for every image generation.

    This process breaks the sequence into individual inference and sampling
    steps, submitting them to the batcher and marshaling final
    results.

    Responsible for a single image.
    """

    def __init__(
        self,
        client: "ClientGenerateBatchProcess",
        gen_req: GenerateReqInput,
        index: int,
    ):
        super().__init__(fiber=client.fiber)
        self.client = client
        self.gen_req = gen_req
        self.index = index
        self.output: Union[TextToImageInferenceOutput, None] = None

    async def run(self):
        exec = SDXLInferenceExecRequest.from_batch(self.gen_req, self.index)
        self.client.batcher.submit(exec)
        await exec.done

        self.output = (
            TextToImageInferenceOutput(exec.response_image)
            if exec.response_image
            else None
        )


from enum import Enum


class MetricPrefix(Enum):
    MILLI = 10 ** (-3)


def seconds_from(
    milliseconds: float = 0,
) -> float:
    return milliseconds * MetricPrefix.MILLI.value


def milliseconds_from(
    seconds: float = 0,
) -> float:
    return seconds / MetricPrefix.MILLI.value


@dataclass
class ServerTimingMetric:
    name: str
    description: str
    duration_in_milliseconds: float

    @property
    def stringified(self) -> str:
        return ";".join(
            [
                self.name,
                f'desc="{self.description}"',
                f"dur={self.duration_in_milliseconds}",
            ]
        )


class HTTPStructuredField(Protocol):
    @property
    def serialized(self) -> str:
        ...


class HTTPDecimalField(HTTPStructuredField, float):
    @property
    def serialized(self) -> str:
        return f"{self}"


class HTTPStringField(HTTPStructuredField, str):
    @property
    def serialized(self) -> str:
        return f'"{self}"'


class HTTPListField(HTTPStructuredField, list[HTTPStructuredField]):
    @property
    def serialized(self) -> str:
        return ",".join([each.serialized for each in self])


class HTTPBooleanField(HTTPStructuredField, bool):
    @property
    def serialized(self) -> str:
        return f"?{1 if self else 0}"


class HTTPParameterField(HTTPStructuredField, tuple[str, HTTPStructuredField]):
    @property
    def serialized(self) -> str:
        [key, value] = self

        if isinstance(value, HTTPBooleanField) and (value == True):
            return key

        return f"{key}={value.serialized}"


class HTTPParametersField(HTTPStructuredField, dict[str, HTTPStructuredField]):
    @property
    def serialized(self) -> str:
        serialized_parameter_fields = [
            HTTPParameterField(each).serialized for each in self.items()
        ]
        return ";".join(serialized_parameter_fields)


def serialized_server_timing_metrics_from(
    given_metrics: HTTPStructuredField,
) -> str:
    return given_metrics.serialized


class ClientGenerateBatchProcess(sf.Process):
    """Process instantiated for handling a batch from a client.

    This takes care of several responsibilities:

    * Tokenization
    * Random Latents Generation
    * Splitting the batch into GenerateImageProcesses
    * Streaming responses
    * Final responses
    """

    __slots__ = [
        "batcher",
        "complete_infeed",
        "gen_req",
        "responder",
    ]

    def __init__(
        self,
        service: SDXLGenerateService,
        gen_req: GenerateReqInput,
        responder: FastAPIResponder,
    ):
        super().__init__(fiber=service.meta_fibers[0].fiber)
        self.gen_req = gen_req
        self.responder = responder
        self.batcher = service.batcher
        self.complete_infeed = self.system.create_queue()

    async def run(self):
        logger.debug("Started ClientBatchGenerateProcess: %r", self)
        prompt_to_png_batch_interval_start = datetime.now()

        try:
            # Launch all individual generate processes and wait for them to finish.
            gen_processes: list[GenerateImageProcess] = []
            for index in range(self.gen_req.num_output_images):
                gen_process = GenerateImageProcess(self, self.gen_req, index)
                gen_processes.append(gen_process)
                gen_process.launch()

            await asyncio.gather(*gen_processes)

            # TODO: stream image outputs
            logging.debug("Responding to one shot batch")

            png_images: list[Base64CharacterEncodedByteSequence] = []

            for index_of_each_process, each_process in enumerate(gen_processes):
                if each_process.output is None:
                    raise Exception(
                        f"Expected output for process {index_of_each_process} but got `None`"
                    )

                each_png_image = png_from(each_process.output.image)
                png_images.append(each_png_image)

            prompt_to_png_batch_interval_end = datetime.now()

            prompt_to_png_batch_timedelta = (
                prompt_to_png_batch_interval_end - prompt_to_png_batch_interval_start
            )

            prompt_to_png_batch_duration_in_milliseconds = milliseconds_from(
                seconds=prompt_to_png_batch_timedelta.total_seconds()
            )

            server_timing_metrics = HTTPListField(
                [
                    HTTPParametersField(
                        pre=HTTPBooleanField(True),
                        desc=HTTPStringField("Batch Pre-processing"),
                        dur=HTTPDecimalField(0),
                    ),
                    HTTPParametersField(
                        infer=HTTPBooleanField(True),
                        desc=HTTPStringField("Batch Inference"),
                        dur=HTTPDecimalField(
                            prompt_to_png_batch_duration_in_milliseconds
                        ),
                    ),
                    HTTPParametersField(
                        post=HTTPBooleanField(True),
                        desc=HTTPStringField("Batch Post-processing"),
                        dur=HTTPDecimalField(
                            prompt_to_png_batch_duration_in_milliseconds
                        ),
                    ),
                ]
            )

            self.responder.send_response(
                JSONResponse(
                    content={
                        "images": png_images,
                    },
                    headers={
                        "Server-Timing": server_timing_metrics.serialized,  # https://w3c.github.io/server-timing
                    },
                    media_type="application/json",
                )
            )
        finally:
            self.responder.ensure_response()
