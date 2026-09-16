# SPDX-License-Identifier: MIT

import ast
import configparser
import json
import logging
from typing import Collection, Optional

from .masking import AbstractMaskingInstruction, MaskingInstruction

logger = logging.getLogger(__name__)


class TemplateMinerConfig:
    def __init__(self) -> None:
        self.engine = "Extractor"
        self.profiling_enabled = False
        self.profiling_report_sec = 60
        self.snapshot_interval_minutes = 5
        self.snapshot_compress_state = True
        self.extractor_extra_delimiters: Collection[str] = []
        self.extractor_sim_th = 0.8
        self.extractor_depth = 4
        self.extractor_max_children = 100
        self.extractor_max_clusters: Optional[int] = None
        self.masking_instructions: Collection[AbstractMaskingInstruction] = []
        self.mask_prefix = "<"
        self.mask_suffix = ">"
        self.parameter_extraction_cache_capacity = 3000
        self.parametrize_numeric_tokens = True

    def load(self, config_filename: str) -> None:
        parser = configparser.ConfigParser()
        read_files = parser.read(config_filename)
        if len(read_files) == 0:
            logger.warning(f"config file not found: {config_filename}")

        section_profiling = 'PROFILING'
        section_snapshot = 'SNAPSHOT'
        section_extractor = 'EXTRACTOR'
        section_masking = 'MASKING'

        self.engine = parser.get(section_extractor, 'engine', fallback=self.engine)

        self.profiling_enabled = parser.getboolean(section_profiling, 'enabled',
                                                   fallback=self.profiling_enabled)
        self.profiling_report_sec = parser.getint(section_profiling, 'report_sec',
                                                  fallback=self.profiling_report_sec)

        self.snapshot_interval_minutes = parser.getint(section_snapshot, 'snapshot_interval_minutes',
                                                       fallback=self.snapshot_interval_minutes)
        self.snapshot_compress_state = parser.getboolean(section_snapshot, 'compress_state',
                                                         fallback=self.snapshot_compress_state)

        extractor_extra_delimiters_str = parser.get(section_extractor, 'extra_delimiters',
                                                fallback=str(self.extractor_extra_delimiters))
        self.extractor_extra_delimiters = ast.literal_eval(extractor_extra_delimiters_str)

        self.extractor_sim_th = parser.getfloat(section_extractor, 'sim_th',
                                            fallback=self.extractor_sim_th)
        self.extractor_depth = parser.getint(section_extractor, 'depth',
                                         fallback=self.extractor_depth)
        self.extractor_max_children = parser.getint(section_extractor, 'max_children',
                                                fallback=self.extractor_max_children)
        self.extractor_max_clusters = parser.getint(section_extractor, 'max_clusters',
                                                fallback=self.extractor_max_clusters)
        self.parametrize_numeric_tokens = parser.getboolean(section_extractor, 'parametrize_numeric_tokens',
                                                            fallback=self.parametrize_numeric_tokens)

        masking_instructions_str = parser.get(section_masking, 'masking',
                                              fallback=str(self.masking_instructions))
        self.mask_prefix = parser.get(section_masking, 'mask_prefix', fallback=self.mask_prefix)
        self.mask_suffix = parser.get(section_masking, 'mask_suffix', fallback=self.mask_suffix)
        self.parameter_extraction_cache_capacity = parser.getint(section_masking, 'parameter_extraction_cache_capacity',
                                                                 fallback=self.parameter_extraction_cache_capacity)

        masking_instructions = []
        masking_list = json.loads(masking_instructions_str)
        for mi in masking_list:
            instruction = MaskingInstruction(mi['regex_pattern'], mi['mask_with'])
            masking_instructions.append(instruction)
        self.masking_instructions = masking_instructions
