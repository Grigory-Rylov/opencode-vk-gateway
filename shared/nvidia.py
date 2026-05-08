"""
NVIDIA GPU utilities for getting GPU information.
"""
import asyncio
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class GPUProcess:
    """Information about a process using GPU."""
    gpu_id: int
    pid: int
    name: str
    memory_usage: str


@dataclass
class GPUInfo:
    """Information about a single GPU."""
    id: int
    name: str
    fan_speed: int  # percentage
    temperature: int  # Celsius
    perf_state: str  # P0-P8
    power_usage: int  # Watts
    power_cap: int  # Watts
    memory_used: int  # MiB
    memory_total: int  # MiB
    gpu_utilization: int  # percentage
    compute_mode: str
    processes: List[GPUProcess] = field(default_factory=list)

    @property
    def memory_percent(self) -> int:
        """Calculate memory usage percentage."""
        if self.memory_total == 0:
            return 0
        return int(self.memory_used * 100 / self.memory_total)

    @property
    def power_percent(self) -> int:
        """Calculate power usage percentage."""
        if self.power_cap == 0:
            return 0
        return int(self.power_usage * 100 / self.power_cap)


@dataclass
class NvidiaInfo:
    """Complete NVIDIA information."""
    driver_version: str
    cuda_version: str
    gpus: List[GPUInfo] = field(default_factory=list)

    @property
    def gpu_count(self) -> int:
        return len(self.gpus)


async def run_nvidia_smi(timeout: int = 30, path: str = "nvidia-smi") -> bytes:
    """
    Execute nvidia-smi command and return raw output.
    
    Args:
        timeout: Timeout in seconds
        path: Path to nvidia-smi executable
    
    Returns:
        Raw bytes output from nvidia-smi
    
    Raises:
        FileNotFoundError: If nvidia-smi not found
        asyncio.TimeoutError: If command times out
        subprocess.SubprocessError: On other errors
    """
    try:
        process = await asyncio.create_subprocess_exec(
            path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout,
        )
        
        if process.returncode != 0:
            raise subprocess.SubprocessError(
                f"nvidia-smi returned non-zero exit code: {process.returncode}",
                process.returncode,
                path,
                None,
                _stderr.decode("utf-8", errors="replace"),
            )
        
        return stdout
    except FileNotFoundError:
        raise FileNotFoundError(
            "nvidia-smi not found. Install NVIDIA drivers."
        )


def parse_nvidia_smi(raw_output: bytes) -> NvidiaInfo:
    """
    Parse nvidia-smi output into NvidiaInfo structure.
    
    Args:
        raw_output: Raw bytes output from nvidia-smi
    
    Returns:
        Parsed NvidiaInfo structure
    
    Raises:
        ValueError: If output cannot be parsed
    """
    text = raw_output.decode("utf-8", errors="replace")
    lines = text.strip().split("\n")
    
    driver_version = "unknown"
    cuda_version = "unknown"
    gpus = []
    
    # Parse header for driver and CUDA versions
    for line in lines:
        if "Driver Version:" in line:
            # Extract driver version from "Driver Version: 595.58.03"
            if "Driver Version:" in line:
                parts = line.split("Driver Version:")
                if len(parts) > 1:
                    driver_version = parts[1].split("|")[0].strip()
        if "CUDA Version:" in line:
            parts = line.split("CUDA Version:")
            if len(parts) > 1:
                cuda_version = parts[1].split("|")[0].strip()
    
    # Parse GPU info
    current_gpu = None
    for line in lines:
        # Check for GPU line: "|   0  NVIDIA GeForce RTX 3090        Off |"
        if "|   " in line and "NVIDIA" in line and "Persistence-M" not in line:
            # Parse GPU number
            if " |   " in line:
                parts = line.split(" |   ")
                if len(parts) > 1:
                    # GPU ID is in parts[0], after "|   "
                    gpu_id_str = parts[0].split("|   ")[1].split(" ")[0].strip()
                    try:
                        gpu_id = int(gpu_id_str)
                    except ValueError:
                        continue
                    
                    # Parse GPU name
                    name = "Unknown"
                    if "NVIDIA" in parts[0]:
                        name_part = parts[0].split("NVIDIA")[1]
                        # Name ends at "Off" or "On" (followed by space and |)
                        if "Off" in name_part:
                            name = "NVIDIA " + name_part.split("Off")[0].strip()
                        elif "On" in name_part:
                            name = "NVIDIA " + name_part.split("On")[0].strip()
                    
                    current_gpu = GPUInfo(
                        id=gpu_id,
                        name=name,
                        fan_speed=0,
                        temperature=0,
                        perf_state="",
                        power_usage=0,
                        power_cap=0,
                        memory_used=0,
                        memory_total=0,
                        gpu_utilization=0,
                        compute_mode="Default",
                    )
                    gpus.append(current_gpu)
        
        # Parse stats line: "|100%   83C    P2            221W /  350W |   22460MiB /  24576MiB |     20%      Default |"
        elif current_gpu and "|  " in line and "MiB" in line and "%" in line:
            # This is a complex line, try to extract values
            try:
                # Fan speed - first percentage at start
                fan_match = _extract_pattern(line, r"(\d+)%\s+")
                if fan_match:
                    current_gpu.fan_speed = int(fan_match)
                
                # Temperature - digits followed by C
                temp_match = _extract_pattern(line, r"\s(\d+)C\s")
                if temp_match:
                    current_gpu.temperature = int(temp_match)
                
                # Performance state
                perf_match = _extract_pattern(line, r"\s+(P\d)\s+")
                if perf_match:
                    current_gpu.perf_state = perf_match
                
                # Power usage
                power_match = _extract_pattern(line, r"\s+(\d+)W\s+/\s+(\d+)W\s")
                if power_match:
                    current_gpu.power_usage = int(power_match[0])
                    current_gpu.power_cap = int(power_match[1])
                
                # Memory usage
                mem_match = _extract_pattern(line, r"(\d+)MiB\s+/\s+(\d+)MiB")
                if mem_match:
                    current_gpu.memory_used = int(mem_match[0])
                    current_gpu.memory_total = int(mem_match[1])
                
                # GPU utilization
                util_match = _extract_pattern(line, r"\s+(\d+)%\s+Default")
                if util_match:
                    current_gpu.gpu_utilization = int(util_match)
            except (ValueError, IndexError):
                pass
            
            current_gpu = None
    
    return NvidiaInfo(
        driver_version=driver_version,
        cuda_version=cuda_version,
        gpus=gpus,
    )


def _extract_pattern(text: str, pattern: str) -> tuple | str | None:
    """Extract pattern from text using simple string operations."""
    import re
    match = re.search(pattern, text)
    if match:
        if len(match.groups()) == 1:
            return match.group(1)
        return match.groups()
    return None


def format_for_vk(info: NvidiaInfo) -> str:
    """
    Format NvidiaInfo as text suitable for VK message.
    
    Args:
        info: Parsed NvidiaInfo structure
    
    Returns:
        Formatted text string
    """
    lines = [
        "🖥️  NVIDIA GPU Information",
        f"Driver: {info.driver_version}",
        f"CUDA: {info.cuda_version}",
        f"GPUs: {info.gpu_count}",
        "─" * 40,
    ]
    
    for gpu in info.gpus:
        lines.append(f"GPU {gpu.id}: {gpu.name}")
        lines.append(f"  Temp: {gpu.temperature}°C | Fan: {gpu.fan_speed}% | {gpu.perf_state}")
        lines.append(f"  Power: {gpu.power_usage}W / {gpu.power_cap}W ({gpu.power_percent}%)")
        lines.append(f"  Memory: {gpu.memory_used} / {gpu.memory_total} MiB ({gpu.memory_percent}%)")
        lines.append(f"  Utilization: {gpu.gpu_utilization}%")
        lines.append("─" * 40)
    
    return "\n".join(lines)


async def get_gpu_info_vk_message(timeout: int = 30) -> tuple[Optional[str], Optional[str]]:
    """
    Get GPU information and format as VK message.
    
    Convenience function that runs nvidia-smi, parses, and formats output.
    
    Args:
        timeout: Timeout in seconds
    
    Returns:
        Tuple of (message_text, error_text) - one will be None
    """
    try:
        raw_output = await run_nvidia_smi(timeout=timeout)
        info = parse_nvidia_smi(raw_output)
        message = format_for_vk(info)
        return message, None
    except FileNotFoundError:
        return None, "❌ nvidia-smi not found. Install NVIDIA drivers."
    except asyncio.TimeoutError:
        return None, (
            "⏱️ nvidia-smi timed out\n"
            "Possible GPU driver error. Check: dmesg | grep nvidia"
        )
    except Exception as e:
        error_msg = str(e).lower()
        if "timeout" in error_msg or "timed out" in error_msg:
            return None, (
                "⏱️ nvidia-smi timed out\n"
                "Possible GPU driver error. Check: dmesg | grep nvidia"
            )
        return None, f"❌ Error: {str(e)[:2000]}"
