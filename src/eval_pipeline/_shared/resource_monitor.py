"""后台采样线程，采集 CPU/内存指标。复制自 benchmark/resource_monitor.py。"""
import os, gc, threading
import psutil


class ResourceMonitor:
    """后台采样线程，采集 CPU/内存指标。

    采集指标:
      - peak_rss_mb: 峰值 RSS (MB)
      - steady_rss_mb: 稳态 RSS (MB) — 运行结束后、gc 后的 RSS
      - peak_cpu_pct: CPU 峰值利用率 (%)
      - avg_cpu_pct: 平均 CPU 利用率 (%)
      - cpu_time_s: CPU 时间 (s) = user + system
      - rss_delta_mb: 内存增量 (MB) = peak_rss - baseline_rss
    """
    def __init__(self):
        self.proc = psutil.Process(os.getpid())
        self.peak_rss_mb = 0.0
        self.baseline_rss_mb = 0.0
        self.peak_cpu_pct = 0.0
        self.avg_cpu_pct = 0.0
        self.cpu_time_s = 0.0
        self.rss_delta_mb = 0.0
        self._cpu_samples = []
        self._stop = threading.Event()
        self._thread = None
        self._cpu_times_start = None

    def start(self):
        self.baseline_rss_mb = self.proc.memory_info().rss / (1024 * 1024)
        ct = self.proc.cpu_times()
        self._cpu_times_start = ct.user + ct.system
        self.proc.cpu_percent(interval=None)  # 初始化基线
        self._cpu_samples = []
        self._stop.clear()
        self._thread = threading.Thread(target=self._sample, daemon=True)
        self._thread.start()

    def _sample(self):
        while not self._stop.is_set():
            try:
                mem = self.proc.memory_info().rss / (1024 * 1024)
                cpu = self.proc.cpu_percent(interval=None)
                if mem > self.peak_rss_mb:
                    self.peak_rss_mb = mem
                if cpu > self.peak_cpu_pct:
                    self.peak_cpu_pct = cpu
                self._cpu_samples.append(cpu)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                break
            self._stop.wait(0.05)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        self.avg_cpu_pct = sum(self._cpu_samples) / len(self._cpu_samples) if self._cpu_samples else 0.0
        gc.collect()
        self.steady_rss_mb = self.proc.memory_info().rss / (1024 * 1024)
        ct = self.proc.cpu_times()
        cpu_time_end = ct.user + ct.system
        self.cpu_time_s = cpu_time_end - self._cpu_times_start if self._cpu_times_start is not None else 0.0
        self.rss_delta_mb = self.peak_rss_mb - self.baseline_rss_mb
