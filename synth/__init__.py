"""合成数据生成器。

设计原则：**规模不必大，但必须带真值。**
没有真值，只能看曲线像不像；有真值，才能回答"她错在哪几个时刻"。
"""

from synth.build import Built, build
from synth.schedule import SCENARIOS, Scenario, generate

__all__ = ["build", "Built", "generate", "Scenario", "SCENARIOS"]
