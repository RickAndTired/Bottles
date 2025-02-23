import os
import subprocess
from typing import NewType

from bottles.backend.utils.generic import detect_encoding  # pyright: reportMissingImports=false
from bottles.backend.managers.runtime import RuntimeManager
from bottles.backend.utils.terminal import TerminalUtils
from bottles.backend.utils.manager import ManagerUtils
from bottles.backend.utils.display import DisplayUtils
from bottles.backend.utils.gpu import GPUUtils
from bottles.backend.globals import Paths, gamemode_available, gamescope_available, mangohud_available
from bottles.backend.logger import Logger

logging = Logger()


class WineEnv:
    """
    Thic class is used to store and return a command environment.
    """
    __env: dict = {}
    __result: dict = {
        "envs": {},
        "overrides": []
    }

    def __init__(self, clean: bool = False):
        self.__env = {}
        if not clean:
            self.__env = os.environ.copy()

    def add(self, key, value, override=False):
        if key in self.__env:
            if override:
                self.__result["overrides"].append(f"{key}={value}")
            else:
                return
        self.__env[key] = value

    def add_bundle(self, bundle, override=False):
        for key, value in bundle.items():
            self.add(key, value, override)

    def get(self):
        result = self.__result
        result["count_envs"] = len(result["envs"])
        result["count_overrides"] = len(result["overrides"])
        result["envs"] = self.__env
        return result

    def remove(self, key):
        if key in self.__env:
            del self.__env[key]

    def is_empty(self, key):
        return len(self.__env.get(key, "").strip()) == 0

    def concat(self, key, values, sep=":"):
        if isinstance(values, str):
            values = [values]
        values = sep.join(values)

        if self.has(key):
            values = self.__env[key] + sep + values
        self.add(key, values)

    def has(self, key):
        return key in self.__env


class WineCommand:
    """
    This class is used to run a wine command with a custom environment.
    It also handles the launch in a terminal or not.
    """

    def __init__(
            self,
            config: dict,
            command: str,
            terminal: bool = False,
            arguments: str = False,
            environment: dict = False,
            comunicate: bool = False,
            cwd: str = None,
            colors: str = "default",
            minimal: bool = False,  # avoid gamemode/gamescope usage
            post_script: str = None
    ):
        self.config = config
        self.minimal = minimal
        self.arguments = arguments
        self.cwd = self.__get_cwd(cwd)
        self.runner = self.__get_runner()
        self.command = self.get_cmd(command, post_script)
        self.terminal = terminal
        self.env = self.get_env(environment)
        self.comunicate = comunicate
        self.colors = colors

    def __get_cwd(self, cwd) -> str:
        config = self.config

        if config.get("IsLayer"):
            bottle = f"{Paths.layers}/{config['Path']}"  # TODO: should not be handled here, just for testing
        elif config.get("Environment", "Custom") == "Steam":
            bottle = config.get("Path")
        else:
            bottle = ManagerUtils.get_bottle_path(config)

        if not cwd:
            '''
            If no cwd is given, use the WorkingDir from the
            bottle configuration.
            '''
            cwd = config.get("WorkingDir")
        if cwd == "":
            '''
            If the WorkingDir is empty, use the bottle path as
            working directory.
            '''
            cwd = bottle

        return cwd

    def get_env(self, environment, return_steam_env: bool = False) -> dict:
        env = WineEnv(clean=return_steam_env)
        config = self.config
        arch = config.get("Arch", None)
        params = config.get("Parameters", None)
        if None in [arch, params]:
            return env.get()["envs"]

        if config.get("IsLayer"):
            bottle = f"{Paths.layers}/{config['Path']}"  # TODO: should not be handled here, just for testing
        elif config.get("Environment", "Custom") == "Steam":
            bottle = config.get("Path")
        else:
            bottle = ManagerUtils.get_bottle_path(config)

        dll_overrides = []
        gpu = GPUUtils().get_gpu()
        ld = []

        # Bottle environment variables
        if config.get("Environment_Variables"):
            for var in config.get("Environment_Variables").items():
                env.add(var[0], var[1], override=True)

        # Environment variables from argument 
        if environment:
            if environment.get("WINEDLLOVERRIDES"):
                dll_overrides.append(environment["WINEDLLOVERRIDES"])
                del environment["WINEDLLOVERRIDES"]

            for e in environment:
                env.add(e, environment[e], override=True)

        # Bottle DLL_Overrides
        if config["DLL_Overrides"]:
            for dll in config.get("DLL_Overrides").items():
                dll_overrides.append(f"{dll[0]}={dll[1]}")

        # Default DLL overrides
        if not return_steam_env:
            dll_overrides.append("mshtml=d")
            dll_overrides.append("winemenubuilder=''")

        # Get Runtime libraries
        if params.get("use_runtime") and not self.terminal:
            _rb = RuntimeManager.get_runtime_env("bottles")
            if _rb:
                logging.info("Using Bottles runtime")
                ld += _rb
            else:
                logging.warning("Bottles runtime was requested but not found")
        if params.get("use_steam_runtime") and not self.terminal:
            _rs = RuntimeManager.get_runtime_env("steam")
            if _rs:
                logging.info("Using Steam runtime")
                ld += _rs
            else:
                logging.warning("Steam runtime was requested but not found")

        # Get Runner libraries
        runner_path = ManagerUtils.get_runner_path(config.get("Runner"))
        for lib in ["lib/wine", "lib32/wine", "lib64/wine"]:
            if os.path.exists(f"{runner_path}/{lib}"):
                ld.append(f"{runner_path}/{lib}")

        # DXVK environment variables
        if params["dxvk"]:
            env.add("WINE_LARGE_ADDRESS_AWARE", "1")
            env.add("DXVK_STATE_CACHE_PATH", bottle)
            env.add("STAGING_SHARED_MEMORY", "1")
            env.add("__GL_DXVK_OPTIMIZATIONS", "1")
            env.add("__GL_SHADER_DISK_CACHE", "1")
            env.add("__GL_SHADER_DISK_CACHE_PATH", bottle)

        # LatencyFleX environment variables
        if params["latencyflex"]:
            _lf_path = ManagerUtils.get_latencyflex_path(config.get("LatencyFleX"))
            _lf_icd = os.path.join(
                _lf_path,
                "layer/usr/share/vulkan/implicit_layer.d/latencyflex.json")
            env.concat("VK_ICD_FILENAMES", _lf_icd)

        # Mangohud environment variables
        if params["mangohud"] and not self.minimal:
            env.add("MANGOHUD", "1")

        # DXVK-Nvapi environment variables
        if not return_steam_env and params["dxvk_nvapi"]:
            conf = self.__set_dxvk_nvapi_conf(bottle)
            env.add("DXVK_CONFIG_FILE", conf)

            # Prevent wine from hiding the Nvidia GPU with DXVK-Nvapi enabled
            if DisplayUtils.check_nvidia_device():
                env.add("WINE_HIDE_NVIDIA_GPU", "1")

        # DXVK HUD environment variable
        if params["dxvk_hud"]:
            env.add("DXVK_HUD", "full")

        # Esync environment variable
        if params["sync"] == "esync":
            env.add("WINEESYNC", "1")

        # Fsync environment variable
        if params["sync"] == "fsync":
            env.add("WINEFSYNC", "1")

        # Futex2 environment variable
        if params["sync"] == "futex2":
            env.add("WINEFSYNC_FUTEX2", "1")

        # Wine debug level
        if not return_steam_env:
            debug_level = "fixme-all"
            if params["fixme_logs"]:
                debug_level = "+fixme-all"
            env.add("WINEDEBUG", debug_level)

        # LatencyFleX
        if not return_steam_env \
                and params["latencyflex"] and params["dxvk_nvapi"]:
            _lf_path = ManagerUtils.get_latencyflex_path(config["LatencyFleX"])
            ld.append(os.path.join(_lf_path, "wine/usr/lib/wine/x86_64-unix"))

        # Aco compiler
        # if params["aco_compiler"]:
        #     env.add("ACO_COMPILER", "aco")

        # FSR
        if params["fsr"]:
            env.add("WINE_FULLSCREEN_FSR", "1")
            env.add("WINE_FULLSCREEN_FSR_STRENGHT", str(params["fsr_level"]))

        # PulseAudio latency
        if params["pulseaudio_latency"]:
            env.add("PULSE_LATENCY_MSEC", "60")

        # Discrete GPU
        if not return_steam_env:
            if params["discrete_gpu"]:
                discrete = gpu["prime"]["discrete"]
                if discrete is not None:
                    gpu_envs = discrete["envs"]
                    for p in gpu_envs:
                        env.add(p, gpu_envs[p])
                    env.add("VK_ICD_FILENAMES", discrete["icd"])

            # VK_ICD
            if not env.has("VK_ICD_FILENAMES"):
                if gpu["prime"]["integrated"] is not None:
                    '''
                    System support PRIME but user disabled the discrete GPU
                    setting (previus check skipped), so using the integrated one.
                    '''
                    env.add("VK_ICD_FILENAMES", gpu["prime"]["integrated"]["icd"])
                else:
                    '''
                    System doesn't support PRIME, so using the first result
                    from the gpu vendors list.
                    '''
                    if "vendors" in gpu and len(gpu["vendors"]) > 0:
                        _first = list(gpu["vendors"].keys())[0]
                        env.add("VK_ICD_FILENAMES", gpu["vendors"][_first]["icd"])
                    else:
                        logging.warning("No GPU vendor found, keep going without setting VK_ICD_FILENAMES..", )

            # Add ld to LD_LIBRARY_PATH
            if ld:
                env.concat("LD_LIBRARY_PATH", ld)

        # DLL Overrides
        env.concat("WINEDLLOVERRIDES", dll_overrides, sep=";")
        if env.is_empty("WINEDLLOVERRIDES"):
            env.remove("WINEDLLOVERRIDES")

        # Wine prefix
        if not return_steam_env:
            env.add("WINEPREFIX", bottle, override=True)

        # Wine arch
        if not return_steam_env:
            env.add("WINEARCH", arch)
        return env.get()["envs"]

    def __get_runner(self) -> str:
        config = self.config
        runner = config.get("Runner")
        arch = config.get("Arch")

        if config.get("Environment", "Custom") == "Steam":
            runner = config.get("RunnerPath", None)

        if runner in [None, ""]:
            return ""

        if "Proton" in runner \
                and "lutris" not in runner \
                and config.get("Environment", "") != "Steam":
            '''
            If the runner is Proton, set the pat to /dist or /files 
            based on check if files exists.
            '''
            _runner = f"{runner}/files"
            if os.path.exists(f"{Paths.runners}/{runner}/dist"):
                _runner = f"{runner}/dist"
            runner = f"{Paths.runners}/{_runner}/bin/wine"

        elif config.get("Environment", "") == "Steam":
            '''
            If the environment is Steam, runner path is defined
            in the bottle configuration and point to the right
            main folder.
            '''
            runner = f"{runner}/bin/wine"

        elif runner.startswith("sys-"):
            '''
            If the runner type is system, set the runner binary
            path to the system command. Else set it to the full path.
            '''
            runner = "wine"

        else:
            runner = f"{Paths.runners}/{runner}/bin/wine"

        if arch == "win64":
            runner = f"{runner}64"

        runner = runner.replace(" ", "\\ ")

        return runner

    def get_cmd(self, command, post_script: str = None, return_steam_cmd: bool = False) -> str:
        config = self.config
        params = config.get("Parameters", {})
        runner = self.runner
        if not return_steam_cmd:
            command = f"{runner} {command}"

        if self.arguments:
            if "%command%" in self.arguments:
                prefix = self.arguments.split("%command%")[0]
                suffix = self.arguments.split("%command%")[1]
                command = f"{prefix} {command} {suffix}"
            else:
                command = f"{command} {self.arguments}"

        if not self.minimal:
            if gamemode_available and params.get("gamemode"):
                if not return_steam_cmd:
                    command = f"{gamemode_available} {command}"
                else:
                    command = f"gamemode {command}"
            if gamescope_available and params.get("gamescope"):
                command = f"{self.__get_gamescope_cmd(return_steam_cmd)} {command}"
            if mangohud_available and params.get("mangohud"):
                if not return_steam_cmd:
                    command = f"{mangohud_available} {command}"
                else:
                    command = f"mangohud {command}"

        if post_script is not None:
            command = f"{command} && sh {post_script}"

        return command

    def __get_gamescope_cmd(self, return_steam_cmd: bool = False) -> str:
        config = self.config
        params = config["Parameters"]
        gamescope_cmd = []

        if gamescope_available and params["gamescope"]:
            gamescope_cmd = [gamescope_available]
            if return_steam_cmd:
                gamescope_cmd = ["gamescope"]
            if params["gamescope_fullscreen"]:
                gamescope_cmd.append("-f")
            if params["gamescope_borderless"]:
                gamescope_cmd.append("-b")
            if params["gamescope_scaling"]:
                gamescope_cmd.append("-n")
            if params["gamescope_fps"] > 0:
                gamescope_cmd.append(f"-r {params['gamescope_fps']}")
            if params["gamescope_fps_no_focus"] > 0:
                gamescope_cmd.append(f"-o {params['gamescope_fps_no_focus']}")
            if params["gamescope_game_width"] > 0:
                gamescope_cmd.append(f"-w {params['gamescope_game_width']}")
            if params["gamescope_game_height"] > 0:
                gamescope_cmd.append(f"-h {params['gamescope_game_height']}")
            if params["gamescope_window_width"] > 0:
                gamescope_cmd.append(f"-W {params['gamescope_window_width']}")
            if params["gamescope_window_height"] > 0:
                gamescope_cmd.append(f"-H {params['gamescope_window_height']}")

        return " ".join(gamescope_cmd)

    def run(self):
        if None in [self.runner, self.env]:
            return

        if self.terminal:
            return TerminalUtils().execute(self.command, self.env, self.colors)

        if self.comunicate:
            try:
                res = subprocess.Popen(
                    self.command,
                    stdout=subprocess.PIPE,
                    shell=True,
                    env=self.env,
                    cwd=self.cwd
                ).communicate()[0]
            except:
                '''
                If return an exception, try to execute the command
                without the cwd argument
                '''
                res = subprocess.Popen(
                    self.command,
                    stdout=subprocess.PIPE,
                    shell=True,
                    env=self.env
                ).communicate()[0]

            enc = detect_encoding(res)
            if enc is not None:
                res = res.decode(enc)
            return res

        try:
            '''
            If the comunicate flag is not set, still try to execute the
            command in comunicate mode, then read the output to catch the
            wine ShellExecuteEx exception, so we can raise it as a bottles
            exception and handle it in other parts of the code.
            '''
            res = subprocess.Popen(
                self.command,
                stdout=subprocess.PIPE,
                cwd=self.cwd,
                shell=True,
                env=self.env
            ).communicate()[0]

            enc = detect_encoding(res)
            if enc is not None:
                res = res.decode(enc)

            if "ShellExecuteEx" in res:
                raise Exception("ShellExecuteEx")
        except:
            # workaround for `No such file or directory` error
            res = subprocess.Popen(self.command, shell=True, env=self.env)
            if self.comunicate:
                return res.communicate()
            return res

    @staticmethod
    def __set_dxvk_nvapi_conf(bottle: str):
        """
        TODO: This should be moved to a dedicated DXVKConf class when
              we will provide a way to set the DXVK configuration.
        """
        dxvk_conf = f"{bottle}/dxvk.conf"
        if not os.path.exists(dxvk_conf):
            # create dxvk.conf if doesn't exist
            with open(dxvk_conf, "w") as f:
                f.write("dxgi.nvapiHack = False")
        else:
            # check if dxvk.conf has the nvapiHack option, if not add it
            with open(dxvk_conf, "r") as f:
                lines = f.readlines()
            with open(dxvk_conf, "w") as f:
                for line in lines:
                    if "dxgi.nvapiHack" in line:
                        f.write("dxgi.nvapiHack = False\n")
                    else:
                        f.write(line)

        return dxvk_conf
