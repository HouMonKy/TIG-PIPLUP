import grp
import os
import pwd
import shutil
import subprocess

import moxing as mox
from naie.context import Context
from naie.datasets import get_data_reference


def get_file_owner_and_group(file_path):
    try:
        # 获取文件的状态信息
        file_info = os.stat(file_path)

        # 获取用户和组的ID
        uid = file_info.st_uid
        gid = file_info.st_gid

        # 将用户ID和组ID转换为名称
        username = pwd.getpwuid(uid)[0]
        groupname = grp.getgrgid(gid)[0]

        return username, groupname

    except FileNotFoundError:
        print(f"文件路径 {file_path} 不存在。")
        return None, None
    except Exception as e:
        print(f"发生错误: {e}")
        return None, None


def setup_paths():
    """设置和创建必要的路径"""
    code_abs_path = os.path.abspath(os.path.dirname(__file__))
    output_path = Context.get_output_path()
    print(f"Output Path: {output_path}")

    paths = {
        'code_dataset_dir': os.path.join(code_abs_path, 'dataset'),
        'model_path': os.path.join(code_abs_path, "model"),
        'out_model_path': os.path.join(code_abs_path, "out_model"),
        's3_output_path': output_path
    }

    for path in paths.values():
        os.makedirs(path, exist_ok=True)

    return paths


def download_dataset(paths, dataset_name="disk_failure_pred_demo.joblib"):
    """下载并复制数据集"""
    try:
        data_reference = get_data_reference(
            "train_model_dataset",
            "nmlabdemo-log-data-v3",
            enable_local_cache=True
        )
        dataset_dir = data_reference.data_path
        print(f"Dataset Directory: {dataset_dir}")

        # 查找并复制数据集文件
        file_path = find_file_or_folder(dataset_dir, dataset_name, is_file=False)
        print("file_path: ", file_path)
        if file_path:
            shutil.copytree(file_path, os.path.join(paths['code_dataset_dir'], dataset_name))
            print(f"Successfully copied {dataset_name}")
        else:
            raise FileNotFoundError(f"Dataset file {dataset_name} not found in {dataset_dir}")

    except Exception as e:
        print(f"Error downloading dataset: {e}")
        raise



def download_model(paths, folder_name="baize_l6_d128_base_zh"):
    """下载并复制模型"""
    try:
        pre_model_path = Context.get('base_model_path')
        print(f"Base Model Path: {pre_model_path}")
        mox.file.copy_parallel(pre_model_path, paths['model_path'])

        # 查找并复制模型文件夹
        model_path = find_file_or_folder(paths['model_path'], folder_name, False)
        if model_path:
            if os.path.exists(os.path.join(paths['model_path'], folder_name)):
                print(f"Successfully copied model folder {folder_name}")
                return
            shutil.copytree(model_path, os.path.join(paths['model_path'], folder_name))
            print(f"Successfully copied model folder {folder_name}")
        else:
            raise FileNotFoundError(f"Model folder {folder_name} not found in {paths['model_path']}")

    except Exception as e:
        print(f"Error downloading model: {e}")
        raise


def find_file_or_folder(root_dir, target_name, is_file=True):
    """查找文件或文件夹"""
    for root, dirs, files in os.walk(root_dir):
        if is_file and target_name in files:
            return os.path.join(root, target_name)
        elif not is_file and target_name in dirs:
            return os.path.join(root, target_name)
    return None


def install_dependencies():
    """安装依赖"""
    try:
        os.system("/home/naie/.local/bin/pip install -r requirements.txt")
        print("Dependencies installed successfully")
    except Exception as e:
        print(f"Error installing dependencies: {e}")
        raise


# def setup_environment():
#     """设置运行环境"""
#     env = os.environ.copy()
#     env["CUDA_VISIBLE_DEVICES"] = "0,1,2,3,4,5,6,7"
#     return env


def execute_command(save_path: str, model_path: str, data_path: str, base_model_folder_name: str):
    """tige多结点环境读取"""
    node_id = str(os.environ.get('RANK'))
    # master_addr = "localhost"
    master_addr = str(os.environ.get('MASTER_ADDR'))
    master_port = str(os.environ.get('MASTER_PORT'))
    local_world_size = int(str(os.environ.get('LOCAL_WORLD_SIZE')))
    world_size = int(str(os.environ.get('WORLD_SIZE')))
    nnodes = str(int(world_size // local_world_size))
    ngpus_per_node = str(8)

    """执行训练命令"""
    file_path = "train_mlm_accelerate.py"
    if os.path.exists(file_path):
        print(f"训练脚本 {file_path} 存在")
    else:
        print(f"训练脚本 {file_path} 不存在")
    user_name, group_name = get_file_owner_and_group(file_path)
    print(f'训练脚本 {file_path} user_name: {user_name}, group_name: {group_name}')
    cmd = [
        "python", "-m", "torch.distributed.launch",
        "--nnodes", nnodes,
        "--node_rank", node_id,
        "--nproc_per_node", ngpus_per_node,
        "--master_addr", master_addr,
        "--master_port", master_port,
        "--use_env",
        file_path,
        "--output_dir", save_path,
        "--model_path", model_path,
        "--data_dir", data_path,
        "--base_model_folder_name", base_model_folder_name,
        # "--skip_training"
    ]
    # env_vars = {"CUDA_VISIBLE_DEVICES": "0,1,2,3,4,5,6,7"}
    # env.update(env_vars)

    try:
        # with open('job.log', 'w') as log_file:
            process = subprocess.run(
                cmd,
                # stdout=subprocess.PIPE,
                # stderr=subprocess.PIPE,
                # text=True,
                # env=env
            )

            # if process.stdout:
            #     log_file.write(process.stdout)
            # else:
            #     log_file.write("日志丢失\n")
            # print(process.stdout)
            print(f"Command completed with return code: {process.returncode}")
            return process.returncode

    except Exception as e:
        print(f"Error executing command: {e}")
        raise


def print_paths(paths):
    """打印所有路径信息"""
    print("\nCurrent Paths:")
    for key, value in paths.items():
        print(f"{key}: {value}")


def main_process():
    """主函数"""
    try:
        paths = setup_paths()
        print_paths(paths)
        save_path = paths['out_model_path']
        model_folder = str(Context.get('pre_train_model_folder'))
        dataset_name = str(Context.get('dataset_name'))

        print(f'模型输出路径： {Context.get_output_path()}')

        download_dataset(paths, dataset_name=dataset_name)
        download_model(paths, folder_name=model_folder)
        install_dependencies()

        # env = setup_environment()
        data_path = os.path.join(paths['code_dataset_dir'], dataset_name)
        model_path = os.path.join(paths['model_path'],model_folder)
        return_code = execute_command(save_path, model_path, data_path, model_folder)

        final_model_path = os.path.join(paths["out_model_path"])
        print("\nTraining completed successfully")
        mox.file.copy_parallel(final_model_path, paths["s3_output_path"])
        print("The trained model has been uploaded to S3.")
        return return_code

    except Exception as e:
        print(f"Error in main function: {e}")
        return 1


if __name__ == "__main__":
    main_process()