# ec2-i4i-capacity-grabber

抢占 **us-east-1** 区域 i4i（存储优化型，Intel Ice Lake + Nitro SSD）EC2 容量的双策略脚本，
用于 Prime Day 等峰值场景下的产能储备。提供两条互补的抢占路线，可按业务形态二选一或组合使用。

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
2. **小机型优先扫描**：默认按 `i4i.large → xlarge → 2xlarge → 4xlarge → 8xlarge` 顺序，逐个 AZ 尝试。小机型更容易抢到碎片化的实时产能。
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

## 用法

### On-Demand 路线（`grab_ondemand.py`）

```bash
# 1) 先 dry-run 看计划（不启任何实例）
python3 grab_ondemand.py --target-cores 8

# 2) 真正抢（启实例并保持 running）
python3 grab_ondemand.py --target-cores 8 --live

# 3) 用完拆除：终止所有打了标签的实例
python3 grab_ondemand.py --terminate-tagged --live
```

参数：
- `--target-cores N`：抢到 N 个 vCPU 就停（默认 8）
- `--types ...`：覆盖默认机型优先级，如 `--types i4i.large i4i.xlarge i4g.large`（可混入 i4g 兜底）
- `--live`：真正执行（不加则 dry-run）
- `--terminate-tagged`：终止所有 `purpose=primeday-i4i-grab` 实例

### ODCR 路线（`grab_odcr.py`）

```bash
# 1) dry-run 看计划（不建预留、不计费）
python3 grab_odcr.py --target-cores 8

# 2) 真正预留（注意：active 预留立刻开始计费！）
python3 grab_odcr.py --target-cores 8 --live

# 可选：加计费保险，N 小时后自动过期
python3 grab_odcr.py --target-cores 8 --live --end-hours 6

# 3) 查看当前所有 active/pending 预留
python3 grab_odcr.py --list

# 4) 释放全部预留（停止计费）—— 等正规 i4i 供给到位后执行
python3 grab_odcr.py --cancel-all --live
```

参数：
- `--target-cores N`：预留到 N 个 vCPU 就停（默认 8）
- `--types ...`：覆盖默认机型优先级
- `--end-hours N`：N 小时后预留自动过期（计费保险，默认不过期直到手动取消）
- `--live`：真正执行（不加则 dry-run）
- `--cancel-all`：取消所有打标签的预留
- `--list`：列出当前预留后退出

> 即时预留（脚本默认用的）**无最低承诺**，可随时取消。
> 若要 Foob 里预订未来某天（如 0703）的产能，需要带 `commitmentDuration`，那要和供给侧另行协商。

---

## Prime Day 实战剧本

1. 让 TAM 把 i4i On-Demand vCPU 配额提到 ≥ 10000。
2. 确认目标账号在各 AZ 有子网（On-Demand 路线需要）。
3. 开抢：
   ```bash
   python3 grab_odcr.py --target-cores 10000 --live
   ```
4. 持续盯：
   ```bash
   python3 grab_odcr.py --list
   ```
5. 等 James 那边正规 i4i 供给到位后，释放预留、停止计费：
   ```bash
   python3 grab_odcr.py --cancel-all --live
   ```

---

## 文件结构

```
.
├── common.py          # 共享工具：AZ/子网发现、机型 offering 探测、退避重试、vCPU 计数、错误分类
├── grab_ondemand.py   # On-Demand 抢占脚本
├── grab_odcr.py       # ODCR 预留抢占脚本
├── requirements.txt   # 依赖（boto3）
└── README.md
```

## 成本参考

- `i4i.large` = **$0.172/小时**（us-east-1，On-Demand，按秒计费，最低 60 秒），2 vCPU。
- 实弹验证已通过：分别启了 1 个实例 + 建了 1 个预留，各持有约 30 秒后清理，总花费约 $0.003。

> 价格数据采集于 2026-06-12，以 AWS Pricing API 实时为准。
