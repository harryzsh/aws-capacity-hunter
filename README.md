# ec2-i4i-capacity-grabber

抢占 i4i（存储优化型，Intel Ice Lake + Nitro SSD）EC2 容量的双策略脚本，
用于 Prime Day 等峰值场景下的产能储备。提供两条互补的抢占路线，可按业务形态二选一或组合使用。

> **区域可配置**：默认 `us-east-1`，所有命令都支持 `--region <region>` 切换到任意区域
> （如 `--region us-west-2`、`--region ap-southeast-1`）。AZ、子网、机型 offering 都会按所选区域自动发现。

| 脚本 | 策略 | 适用场景 |
|------|------|----------|
| `grab_ondemand.py` | 普通 On-Demand（`RunInstances`） | 负载稳定、实例**持续运行**。实例 running 即占住产能；一旦 stop/terminate，产能立刻回到公共池 |
| `grab_odcr.py` | On-Demand 容量预留 ODCR（`CreateCapacityReservation`） | 业务**有 stop/restart 周期**或迁移窗口。预留会把产能锁在你名下，即使实例停了也不丢；代价是 active 预留按 On-Demand 价**持续计费**（无论是否填充） |

> ⚠️ **ODCR 不会在容量池里插队**——它和普通 On-Demand 抢的是同一个池子，没有优先级。
> 它唯一的价值是「抢到后即使实例停了也不还回去」。如果负载是长期持续的，直接用 `grab_ondemand.py` 更简单、效果一样。

---

## 工作原理

两个脚本共享 `common.py`，核心思路一致：

1. **自动发现** 区域内所有可用 AZ、各 AZ 的可用子网、以及每个实例类型在哪些 AZ 真正被提供（跳过不可能的调用）。
2. **默认只抢 `i4i.16xlarge`（64 核）**：一台就是一大块核，凑够目标核数所需的实例/预留数量和 API 调用最少。需要降级兜底时，用 `--types` 显式列出其他机型，脚本会**自动按 vCPU 从大到小排序**后逐个尝试（你不用关心写的顺序）。
3. **逐个抢**：每次只 `count=1`，抢到一个就累加 vCPU，直到达到 `--target-cores` 目标。
4. **智能处理**：
   - 没产能（`InsufficientInstanceCapacity` 等）→ 记一笔，换下一个 AZ/机型，不算失败。
   - 被限流（`Throttling`）→ 指数退避 + 抖动后重试同一目标。
   - 其他错误 → 视为致命，立即抛出。
5. **进度统计**：结束后打印实际抢到多少 vCPU、分布在哪些 AZ。

### 两条路线的 AZ 差异
- **On-Demand** 需要子网才能 `RunInstances`，所以只扫**有子网的 AZ**（默认 VPC 通常只有部分 AZ 有默认子网）。
- **ODCR** 创建预留**不需要子网**，所以扫**全部 AZ**；只有真正往预留里启实例时才需要对应 AZ 有子网。

---

## 前置条件

1. **AWS 凭证**：通过环境变量、`~/.aws/credentials` 或 IAM 角色配置好，且有权限调用 EC2 / SSM。
2. **Python 3.8+** 和 `boto3`：

   ```bash
   pip install -r requirements.txt
   ```

3. **配额（关键！）**：真实抢 10000 核之前，必须先通过 TAM 把 i4i 的 On-Demand vCPU 配额提到 ≥ 目标值，
   否则会先撞配额上限（`VcpuLimitExceeded`）而不是产能上限。
4. **子网**（仅 On-Demand 路线需要）：目标账号在每个想抢的 AZ 都要有可用子网。

---

## 安全机制

- **默认 dry-run**：不加 `--live` 时只做参数 / IAM 校验（`DryRun=True`），**不会真的启实例或建预留**。
- **打标签追踪**：所有资源都打 `purpose=primeday-i4i-grab` 标签，方便定位和清理。
- **一键清理**：两个脚本都自带拆除命令，防止资源泄漏 / 持续计费。

---

## 最重要的一个开关：`--live`

两个脚本**默认都是演练模式（dry-run）**，只校验权限和参数、打印计划，**不会真的动任何资源、不花一分钱**。
只有当你加上 `--live`，才会真正去抢。先不加 `--live` 跑一遍看计划，确认无误再加 `--live` 实弹，这是最安全的用法。

```bash
python3 grab_ondemand.py --target-cores 8           # 演练：只看计划，不启实例
python3 grab_ondemand.py --target-cores 8 --live    # 实弹：真的启实例
```

---

## 脚本一：`grab_ondemand.py`（普通 On-Demand 抢占）

**做什么**：直接启动 i4i 实例。实例只要保持 running 就占住产能；一旦 stop/terminate，产能立刻还回公共池。
**适合**：负载稳定、实例长期持续运行的场景。

### 常用命令

```bash
# 1) 演练，看会抢哪些机型、哪些 AZ（不启任何实例）
python3 grab_ondemand.py --target-cores 8

# 2) 实弹，真正启实例并保持 running
python3 grab_ondemand.py --target-cores 10000 --live

# 3) 用完清理，终止本脚本启的所有实例
python3 grab_ondemand.py --terminate-tagged --live
```

### 全部参数

| 参数 | 作用 | 默认值 | 示例 |
|------|------|--------|------|
| `--region R` | 在哪个 AWS 区域抢 | `us-east-1` | `--region us-west-2` |
| `--target-cores N` | 抢够 N 个 vCPU 就停下 | `8` | `--target-cores 10000` |
| `--types ...` | 机型列表，脚本**自动按 vCPU 从大到小排序**（顺序随便写）。不传 = 只抢 `i4i.16xlarge`。传多个 = 允许降级兜底（可混 i4g） | 只 `i4i.16xlarge` | `--types i4i.16xlarge i4i.8xlarge i4g.16xlarge` |
| `--azs ...` | 锁定只在这些 AZ 抢（写 AZ 名字）。不传 = 区域内所有 AZ | 所有 AZ | `--azs us-east-1c us-east-1d` |
| `--live` | 真正启实例。**不加 = 只演练不启** | 关闭（演练） | `--live` |
| `--watch` | 24×7 死等模式：循环重扫，直到抢够 `--target-cores` 才停（产能是间歇放出来的，盯着才抢得到） | 关闭（扫一遍就退出） | `--watch` |
| `--interval N` | `--watch` 模式下每轮之间隔几秒 | `60` | `--interval 30` |
| `--terminate-tagged` | 一键终止本脚本启的所有实例（清理用） | 关闭 | `--terminate-tagged --live` |
| `-h` / `--help` | 显示帮助 | — | `--help` |

---

## 脚本二：`grab_odcr.py`（容量预留 ODCR 抢占）

**做什么**：创建容量预留，把产能锁在你名下。即使没启实例、或实例停了，产能也不会还回去。
**适合**：业务有 stop/restart 周期、或迁移窗口需要「停了也不丢产能」的场景。
**⚠️ 注意**：active 预留**立刻按 On-Demand 价持续计费**（无论里面有没有跑实例），用完务必 `--cancel-all` 释放。

### 常用命令

```bash
# 1) 演练，看会预留哪些机型（不建预留、不计费）
python3 grab_odcr.py --target-cores 8

# 2) 实弹预留（⚠️ 一加 --live 就立刻开始计费）
python3 grab_odcr.py --target-cores 10000 --live

# 3) 加计费保险：6 小时后预留自动过期释放，防止忘记取消
python3 grab_odcr.py --target-cores 10000 --live --end-hours 6

# 4) 随时查看当前持有哪些预留
python3 grab_odcr.py --list

# 5) 释放全部预留、停止计费（正规 i4i 供给到位后执行）
python3 grab_odcr.py --cancel-all --live
```

### 全部参数

| 参数 | 作用 | 默认值 | 示例 |
|------|------|--------|------|
| `--region R` | 在哪个 AWS 区域抢 | `us-east-1` | `--region us-west-2` |
| `--target-cores N` | 预留够 N 个 vCPU 就停下 | `8` | `--target-cores 10000` |
| `--per-az-cores N` | **均衡模式**：每个 AZ 各封顶 N 个 vCPU，某个 AZ 凑满就跳过、继续抢其余 AZ，让预留在各 AZ 间均匀分布（对齐 ASG 的 50/50 调度）。单独设此项、`--target-cores` 留默认时，总目标自动 = `N × AZ数` | 关闭（不均衡） | `--per-az-cores 5000` |
| `--types ...` | 机型列表，脚本**自动按 vCPU 从大到小排序**（顺序随便写）。不传 = 只预留 `i4i.16xlarge` | 只 `i4i.16xlarge` | `--types i4i.16xlarge i4i.8xlarge` |
| `--azs ...` | 锁定只在这些 AZ 预留（写 AZ 名字）。不传 = 区域内所有 AZ | 所有 AZ | `--azs us-east-1c us-east-1d` |
| `--end-hours N` | N 小时后预留**自动过期释放**（计费保险，防止忘关） | 不过期，直到手动取消 | `--end-hours 6` |
| `--live` | 真正建预留。**不加 = 只演练不建**。⚠️ 加了立刻计费 | 关闭（演练） | `--live` |
| `--watch` | 24×7 死等模式：循环重扫，直到预留够 `--target-cores` 才停 | 关闭（扫一遍就退出） | `--watch` |
| `--interval N` | `--watch` 模式下每轮之间隔几秒 | `60` | `--interval 30` |
| `--cancel-all` | 取消所有预留、**停止计费**（清理用） | 关闭 | `--cancel-all --live` |
| `--list` | 只查看当前持有的预留，不增不删 | 关闭 | `--list` |
| `-h` / `--help` | 显示帮助 | — | `--help` |

> 脚本默认用的是「即时预留」，**无最低承诺，可随时取消**。
> 若要预订未来某天（如 0703）的产能，需要带 `commitmentDuration`，那要和供给侧另行协商。

---

## 两个脚本共有的机型策略

**默认只抢 `i4i.16xlarge`（64 核）**——一台一大块核，凑够目标所需的实例/预留数最少。
需要降级兜底时，用 `--types` 列出多个机型即可，脚本会**自动按 vCPU 从大到小排序**后逐个尝试（你写的顺序无所谓，未知机型会被自动忽略并告警）。
内置的 vCPU 对照（用 `--types` 自定义时参考）：

| 机型 | vCPU | 内存 | 本地 NVMe SSD |
|------|------|------|----------|
| `i4i.large` | 2 | 16 GiB | 1 × 468 GB |
| `i4i.xlarge` | 4 | 32 GiB | 1 × 937 GB |
| `i4i.2xlarge` | 8 | 64 GiB | 1 × 1,875 GB |
| `i4i.4xlarge` | 16 | 128 GiB | 1 × 3,750 GB |
| `i4i.8xlarge` | 32 | 256 GiB | 2 × 3,750 GB |
| `i4i.16xlarge` | 64 | 512 GiB | 4 × 3,750 GB |
| `i4i.32xlarge` | 128 | 1,024 GiB | 8 × 3,750 GB |

---

## Prime Day 实战剧本

### ⭐ 本次 Prime Day 标准命令（10000 vCPU 总数，两 AZ 均衡）

> 目标是 **10000 vCPU 总数**（不是 10000 台），均匀铺在 `us-east-1b` / `us-east-1d` 两个 AZ。
> 每 AZ 各封顶 `10000 ÷ 2 = 5000` vCPU；i4i.16xlarge = 64 vCPU/台，即每 AZ 约 78 台、合计约 156 台。
> 用 `--per-az-cores` 设单 AZ 上限，脚本会**自动把总目标算成 `5000 × 2 = 10000`**，你只需填一个数。

```bash
# 第 0 步：先演练（不加 --live），确认 AZ、机型、每 AZ 上限、总目标都对
python3 grab_odcr.py \
  --azs us-east-1b us-east-1d \
  --per-az-cores 5000

# 第 1 步：实弹 24×7 死等，两 AZ 均衡抢满 10000 vCPU（⚠️ 一加 --live 立刻计费）
python3 grab_odcr.py \
  --azs us-east-1b us-east-1d \
  --per-az-cores 5000 \
  --live --watch --interval 30
```

- **`--per-az-cores 5000`**：每个 AZ 各到 5000 vCPU 就停抢、跳过去抢另一个 AZ，预留天然均衡（对齐 ASG 的 50/50 调度），不会单边堆出空转浪费。
- **`--watch --interval 30`**：产能是间歇放出来的，每 30 秒重扫一次、死等攒满，已抢到的累加、每轮只补差额。
- **不加 `--end-hours`**：Prime Day 要长期持有产能，不能让预留中途自动过期；用完手动 `--cancel-all` 释放。
- 挂在 `tmux` / `nohup` 里长期跑，`Ctrl-C` 随时安全退出，已抢资源不受影响。

> 想加计费保险（比如怕忘关）可附 `--end-hours 12`，但要确保大于你实际持有时长，否则预留会提前释放。
> 想允许降级兜底（16xl 抢不到就往下），加 `--types i4i.16xlarge i4i.8xlarge i4i.4xlarge`（脚本自动按 vCPU 从大到小排序）。

### 完整步骤

1. 让 TAM 把 i4i On-Demand vCPU 配额提到 ≥ 10000，否则会先撞配额（`VcpuLimitExceeded`）而不是产能上限。
2. 确认目标账号在 `us-east-1b` / `us-east-1d` 有子网（ODCR 建预留不需要子网，但真正往预留里启实例时需要）。
3. 跑上面的 ⭐ 标准命令（先演练后实弹）。
4. 持续盯进度：
   ```bash
   python3 grab_odcr.py --list                 # 当前持有哪些预留
   tail -f logs/grab_odcr.log                   # 实时流水，看各 AZ 分布
   wc -l logs/grabs.jsonl                        # 已抢到多少笔
   ```
5. 等 James 那边正规 i4i 供给到位后，释放预留、停止计费：
   ```bash
   python3 grab_odcr.py --cancel-all --live
   ```

---

## ASG 接预留配置（客户侧必读）

脚本只负责**把预留抢到手**。预留抢到后，能不能被你的 Auto Scaling Group（ASG）自动「吃掉」、扩容时实例真正落进预留，**取决于客户侧的 ASG 配置**。这一节是开 ASG 前客户必须确认的事项。

### 核心开关：`capacity-reservations-first`

在 ASG 上设置 Capacity Reservation 偏好（**设在 ASG 上，不是启动模板上**）：

```bash
aws autoscaling create-auto-scaling-group \
  --auto-scaling-group-name <你的ASG名> \
  --launch-template "LaunchTemplateId=<LT-ID>,Version=\$Latest" \
  --vpc-zone-identifier "<1b子网>,<1d子网>" \
  --capacity-reservation-specification "CapacityReservationPreference=capacity-reservations-first" \
  ...

# 已有 ASG 改配置：
aws autoscaling update-auto-scaling-group \
  --auto-scaling-group-name <你的ASG名> \
  --capacity-reservation-specification "CapacityReservationPreference=capacity-reservations-first"
```

| 偏好值 | 行为 | 适用 |
|--------|------|------|
| `capacity-reservations-first` | **优先**用匹配的 open 预留，预留用光了**自动回落到普通 On-Demand**（软兜底，不会扩容失败） | ⭐ Prime Day 推荐：有预留就吃，没预留也不挂 |
| `capacity-reservations-only` | 只用预留，没预留就**扩容失败** | 太硬，不推荐（预留不够时实例起不来） |
| `none` / `default` | 不主动用预留 | 不适用 |

> 用 `capacity-reservations-first` + `open` 模式预留：ASG **不需要**指定具体预留 ID，任何属性匹配的实例都会自动掉进预留。这就是为什么 `grab_odcr.py` 默认建 `open` 预留——客户 ASG 零改动即可吸纳。

### 实例要落进预留，4 个属性必须 100% 对齐

预留和 ASG 启动的实例，下面 4 项**少一项对不上，实例就掉不进预留**（会静默走普通 On-Demand）：

| 属性 | 必须一致 | 本次配置 |
|------|----------|----------|
| **机型** instance type | 预留机型 = LT 机型 | `i4i.16xlarge` |
| **平台** platform (OS) | 预留 platform = 实例 OS | `Linux/UNIX`（AL2023） |
| **可用区** AZ | 预留 AZ ∈ ASG 子网所在 AZ | `us-east-1b` / `us-east-1d` |
| **租户** tenancy | 预留 tenancy = 实例 tenancy | `default`（共享） |

### 客户开 ASG 前 checklist

- [ ] ASG 设了 `CapacityReservationPreference=capacity-reservations-first`
- [ ] ASG 的 `--vpc-zone-identifier` 横跨预留所在的两个 AZ（1b + 1d），且每个 AZ 都有可用子网
- [ ] 启动模板的机型 = 预留机型（`i4i.16xlarge`）
- [ ] 启动模板的 AMI 架构 = 机型架构（i4i 是 x86_64，别配成 ARM AMI）
- [ ] 启动模板平台 = `Linux/UNIX`，tenancy = `default`
- [ ] 预留是 `open` 模式（`grab_odcr.py` 默认就是，无需 ASG 指定预留 ID）

### ⚠️ 常见坑

- **属性对不上 = 双倍计费**：实例掉不进预留时不会报错，而是静默起一台普通 On-Demand。结果你**同时付**空转预留的钱 + On-Demand 实例的钱。开 ASG 前务必核对上面 4 个属性。
- **AMI 架构不匹配**：i4i 是 Intel x86_64，启动模板若配了 ARM/Graviton 的 AMI，实例根本起不来。
- **预留用了 `targeted` 而非 `open`**：targeted 模式要求实例显式指定预留 ID，ASG 不会自动吸纳。本脚本默认 `open`，不要改成 targeted。
- **ASG 50/50 调度 vs 预留分布**：ASG 默认往实例数少的 AZ 放，最终≈50/50。所以预留也要 50/50 均衡铺（用 `--per-az-cores`），否则多的那边预留空转、少的那边缺口落到 On-Demand。

---

## Smoke Test 验证报告

为了在真金白银抢 i4i 之前，先确认「**open 预留 + ASG `capacity-reservations-first` → 实例自动落进预留**」这套机制确实成立，跑了一次端到端 smoke test。

### 关键设计决策：用 t3.micro，不用 i4i.16xlarge

| 维度 | 说明 |
|------|------|
| **验证目标** | 验的是 ASG 吃预留的**机制**，不是 i4i 容量本身 |
| **为什么换机型** | `capacity-reservations-first` 的匹配逻辑**与机型无关**——对 t3.micro 成立，对 i4i.16xlarge 必然同样成立 |
| **成本** | t3.micro 2 台 + 2 个预留跑几分钟，全程 **< $0.05**；i4i.16xlarge 2 台约 $11/小时，且本来就可能抢不到容量，反而干扰结论 |
| **容量** | t3.micro 容量稳拿，不会卡在「抢不到」上，确保测的是机制而非运气 |

> ⚠️ 注意区分：本测试证明的是**机制链路**。Prime Day 真跑 i4i 时，能不能**抢到** i4i 预留是另一回事（取决于 AWS 池子有无货，靠 `grab_odcr.py --watch` 死等解决）。但只要预留抢到了，ASG 一定能把它吃进去——这一点已实锤。

### 测试步骤（我实际做的）

环境：`us-east-1`，账户 `476114114317`，默认 VPC（`vpc-02f8...52d0`，原本只有 1c 一个子网）。所有资源打 `purpose=primeday-smoke-test` 标签，便于一键拆除。

1. **建子网**：默认 VPC 在 1b/1d 没子网，临时各建一个（`172.31.16.0/20`@1b、`172.31.32.0/20`@1d）。
2. **建 open 预留**：在 1b、1d 各建 1 个 `t3.micro` 容量预留，`platform=Linux/UNIX`、`tenancy=default`、`instance-match-criteria=open`（与生产 ODCR 同模式）。
3. **建启动模板**：AL2023 AMI（x86_64）、`t3.micro`、默认 SG。
4. **建 ASG**：横跨两个子网，`min=0 / max=2 / desired=0`，`CapacityReservationPreference=capacity-reservations-first`。
5. **记录基线**：两个预留 `Available=1`、未使用。
6. **触发扩容**：`set-desired-capacity --desired-capacity 2`，等约 90 秒实例起来。
7. **双向取证**（见下）。
8. **拆除**：删 ASG（force，连带终止实例）→ 取消两个预留（停计费）→ 删启动模板 → 删游离 ENI → 删两个子网。

### 结果：✅ 通过

两侧证据对得上，机制确认无误：

| 视角 | us-east-1b | us-east-1d |
|------|-----------|-----------|
| 实例的 `CapacityReservationId` | → `cr-…aa28` ✓ | → `cr-…459` ✓ |
| 预留 `Available`（扩容前 → 后） | 1 → **0** | 1 → **0** |

- **实例侧**：两台实例的 `CapacityReservationId` 字段直接写着对应 AZ 的预留 ID——实例自己记录了它落进了哪个预留。
- **预留侧**：两个预留的可用槽位同步从 1 掉到 **0**，证明槽位被实例占满。
- > 小注：CLI 表格里 `UsedInstanceCount` 字段渲染成 `null`/`None` 是 API 的显示怪癖，但 `Total(1) − Available(0) = 1` 在数学上就是「被占用 1 个」，结论不受影响。

### 清理结果：零残留

拆除后扫描 5 类资源（实例 / 预留 / 启动模板 / ASG / 子网），**全部为空**，无任何持续计费资源遗留。子网首次删除时因实例刚终止、ENI 未释放报 `DependencyViolation`，等 ENI 被 AWS 自动回收后重删成功——这是正常的资源释放时序。

---

## 24×7 死等模式（`--watch`）

产能不是一直有的，AWS 会**间歇性**地把回收的 i4i 放回池子——可能凌晨某几分钟突然有一批，几秒后又被别人抢光。
单次扫描很可能空手而归。`--watch` 让脚本**循环重扫**，每隔 `--interval` 秒再扫一遍，直到抢够 `--target-cores` 才停，这才是真正能把产能攒起来的用法。

```bash
# On-Demand：每 30 秒重扫一次，死等到凑够 10000 vCPU
python3 grab_ondemand.py --target-cores 10000 --live --watch --interval 30

# ODCR：同理，配合 --end-hours 做计费保险
python3 grab_odcr.py --target-cores 10000 --live --watch --interval 30 --end-hours 6
```

- 已抢到的会累加，每轮只补差额，不会重复抢。
- `Ctrl-C` 随时安全退出，已抢到的资源不受影响（已记在日志和台账里）。
- 适合挂在 `tmux` / `screen` / `nohup` 里长期跑，或交给 systemd / cron 托管。

---

## 日志与抢占台账

脚本会把每一轮运行**全程记录到日志**，无需任何开关、零外部依赖，全部落在本地 `logs/` 目录：

| 文件 | 内容 | 格式 |
|------|------|------|
| `logs/grab_ondemand.log` / `logs/grab_odcr.log` | 人读的完整运行流水：每轮扫了哪些 AZ/机型、抢到/没抢到、限流退避等 | 文本，自动轮转（单文件 5 MB，保留 5 份，绝不撑爆磁盘） |
| `logs/grabs.jsonl` | **抢占台账**：每真正抢到一台就追加一行 JSON，方便事后统计、对账、喂给其他工具 | JSON Lines（一行一条） |

`grabs.jsonl` 每行的字段：

```json
{"ts": "2026-06-13T07:12:16Z", "via": "ondemand", "instance_type": "i4i.8xlarge",
 "az": "us-east-1a", "region": "us-east-1", "vcpu": 32, "total_vcpu": 32, "target_vcpu": 10000}
```

| 字段 | 含义 |
|------|------|
| `ts` | 抢到时刻（UTC ISO8601） |
| `via` | 路线：`ondemand` 或 `odcr` |
| `instance_type` | 抢到的机型 |
| `az` | 落在哪个可用区 |
| `region` | 区域 |
| `vcpu` | 这一台贡献的 vCPU |
| `total_vcpu` | 累计已抢到的 vCPU |
| `target_vcpu` | 本次目标 vCPU |

> **演练（dry-run）不写台账**——`grabs.jsonl` 里只会有真正计费的抢占记录，干净可审计。
> 想看当前进度：`tail -f logs/grab_ondemand.log`；想统计抢到多少台：`wc -l logs/grabs.jsonl`。

---

## 文件结构

```
.
├── common.py          # 共享工具：AZ/子网发现、机型 offering 探测、退避重试、vCPU 计数、错误分类、日志与台账
├── grab_ondemand.py   # On-Demand 抢占脚本
├── grab_odcr.py       # ODCR 预留抢占脚本
├── logs/              # 运行日志（自动轮转）+ 抢占台账 grabs.jsonl（首次运行自动生成）
├── requirements.txt   # 依赖（boto3）
└── README.md
```

## 成本参考

- `i4i.large` = **$0.172/小时**（us-east-1 参考价，On-Demand，按秒计费，最低 60 秒），2 vCPU。价格随区域不同，以 AWS Pricing API 实时为准。
- 实弹验证已通过：分别启了 1 个实例 + 建了 1 个预留，各持有约 30 秒后清理，总花费约 $0.003。
- `--region` 已在 us-east-1 / us-west-2 验证可正常发现各自的 AZ 与机型 offering。
