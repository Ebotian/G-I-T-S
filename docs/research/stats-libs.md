# 统计库：用哪个、不用哪个、在哪用

> 起因：手写了 Lomb-Scargle、核密度、BOCPD，然后被问"Python 有那么多成熟统计库吧"。
> 答案是"有，但**要看它在哪个进程里跑**"——这台机器只有 1.6 GiB。

## 1. 实测（都是量出来的，不是估的）

| 库 | 本机 import | ECS（2C/1.6G, py3.10） import | 常驻 RSS |
|---|---|---|---|
| numpy | 60 ms | **188 ms** | ~30 MB |
| scipy.signal / scipy.stats | 700 / 263 ms | — | ~95 MB |
| pandas | 112 ms | — | 73 MB |
| **statsmodels.api** | 877 ms | **2119 ms** | **152 MB** |

ECS 上装 numpy + statsmodels：磁盘 **361 MB**，pip 31 s（走阿里云镜像）。

> 第一约束是**允许哪些库**：ghost 只允许 stdlib + numpy。
> 早先写的「90 MB 预算」其实是先决定不用 statsmodels（152 MB）、再倒推出来的数——
> 它挡不住「这个库有用」，只挡得住「这个库很大」。现在 ghost 实测 42 MB。
>
> 所以：**允许清单是约束，RSS 和 import 耗时是观测指标。**
> 所以结论不是"用不用"，是"在哪用"。

## 2. 三层依赖纪律

| 进程 | 允许 | 理由 |
|---|---|---|
| `ghost`（常驻，处理对话） | **stdlib + numpy** | 允许清单是约束；RSS < 200 MB、import < 500 ms 是观测指标 |
| `shell`（通道/采集） | stdlib + numpy | 同上 |
| `dream`（夜间批处理） | **statsmodels / scipy / pandas 随意** | 一晚一次，2 秒 import 无所谓；跑完就退 |
| `synth` / tests | 任意 | 开发期 |

**落地方式**：拟合在 `dream` 里做完，**只把系数存进 `presence_model` 表**；
`ghost` 读系数做一次点积（numpy，微秒级）。见 `gits/presence.py`。

这条纪律有测试守着：`test_ghost_不导入_statsmodels` 用子进程检查
`import gits.presence` 之后 `'statsmodels' in sys.modules` 必须为 False。

## 3. 逐项决定

| 我们现在 | 成熟方案 | 决定 |
|---|---|---|
| 手写 `lomb_scargle` | `scipy.signal.lombscargle` | **已验证等价**（同一份数据：相关 1.000000、峰位一致），但**暂不换**——20 行、无依赖，且只是一次性判断 |
| 手写 `WeeklyKDE` | `scipy.stats.vonmises` / sklearn `KernelDensity` | **保留**：环形核 20 行；sklearn 不支持周期边界。但**降级为稀疏数据兜底** |
| 周期检验的零分布 | **泊松 GLM 的 p 值**（statsmodels） | **换**。见 §4 |
| 手写 `bocpd` | `ruptures` / `bayesian_changepoint_detection` | **待定**。我们需要**在线**判断（她不能等到明天才知道今晚不对）；ruptures 是离线算法。保持手写，用 ruptures 在 `dream` 里交叉校验 |
| `hazard`（沉默多久算异常） | `lifelines` | **待定**。等有真实数据再决定要不要上生存分析 |
| 霍克斯互激 | `tick` / `pyhawkes` | **不上**。目前用 `elicitation_ratio` 基线（§14.5 先基线后模型）；真要时 2×2 指数核自己写 60 行，比装 C++ 库划算 |
| 记忆衰减 | `fsrs`（open-spaced-repetition 的 Python 版） | **可换**。等攒够 500 次真实接触再谈拟合（§5.1.2） |

## 4. 换掉核密度：泊松 GLM + 傅里叶项

这是这次最有价值的一处替换。核密度只能给一条曲线，**显著性还得另配零分布**
（而我配错了两次，见 `rhythm.md`）。泊松 GLM 直接给：

    counts ~ Poisson(exp(β0 + Σ β·[cos,sin](2πmt/24) + ...(2πmt/168) + β_weekend))

- **p 值**：24h 项显著不显著，一句话说完
- **协变量**：周末、节点在线、天气，直接加一列
- **可解释**：系数就是振幅和相位
- **6 ms** 拟合（本机）

实测（60 天合成数据）：

| 场景 | dispersion | p(24h) | 结论 |
|---|---|---|---|
| `regular` | 3.01 | 1.25e-21 | 强日周期 |
| `chaotic` | 3.39 | 1.57e-02 | 判不出来（正确） |

## 5. 两个必须知道的坑（都踩过）

### 5.1 谐波会重复，导致设计矩阵秩亏

24h 取 2 阶时，二次谐波 cos(4πt/24) **就是** cos(2πt/12)，也就是 12h 项。
再单独加一个 12h 项 → 矩阵秩亏，statsmodels 直接警告"参数不可唯一确定"，
系数变成任意解。

**规则**：要么列基频让阶数生成谐波，要么列互不整除的周期，**不能混**。

### 5.2 泊松 GLM 默认假设 var = mean，成簇数据会把 p 值压成假的极小

我们的数据是成簇的（一次会话连发五条）。泊松假设独立同分布，
标准误被低估：

    chaotic 序列：scale=1     → p(24h) = 8.8e-06    ← 假的"极显著"
                  scale="X2"  → p(24h) = 1.6e-02    ← 拟泊松修正后

**修法**：`.fit(scale="X2")`（拟泊松），离散度自动估计并写进
`presence_model.dispersion`——**这个数本身就是成簇程度的量化指标**，
3.0 意味着方差是均值的 3 倍。

> 顺带解释了"间隔自助"零分布为什么有效：它是在**非参数地**处理同一件事——
> 保留成簇、只破坏周期。拟泊松是参数版的同一个修法。两条路都对，
> 但库比手写更不容易走偏。

## 6. 依赖政策

1. **新依赖要有实测数字**（import 耗时、RSS、磁盘），贴进本文件，再决定放哪一层
2. **`ghost` 的依赖只减不增**。想加库，先证明这件事不能在 `dream` 里做完
3. 版本锁在 `pyproject.toml`；`dream` 的额外依赖放 `[project.optional-dependencies]`
4. **换掉手写实现的前提是"它算得对"**：先用同一份数据跑对照（像 `lomb_scargle` 那样），
   相关性和峰位一致再换
