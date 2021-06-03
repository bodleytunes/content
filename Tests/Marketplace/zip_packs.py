import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor
import shutil
import sys
import logging
from time import sleep
from zipfile import ZipFile
from Tests.Marketplace.marketplace_services import init_storage_client
from Tests.Marketplace.marketplace_constants import IGNORED_FILES, PACKS_FULL_PATH
from Tests.scripts.utils.log_util import install_logging
from demisto_sdk.commands.common.tools import LooseVersion, str2bool
import subprocess
from pathlib import Path

ARTIFACT_NAME = 'content_marketplace_packs.zip'
MAX_THREADS = 4
BUILD_GCP_PATH = 'content/builds'


def option_handler():
    """Validates and parses script arguments.

    Returns:
        Namespace: Parsed arguments object.

    """
    parser = argparse.ArgumentParser(description="Zip packs from a GCP bucket.")
    # disable-secrets-detection-start
    parser.add_argument('-a', '--artifacts_path', help="Path of the CircleCI artifacts to save the zip file in",
                        required=False)
    parser.add_argument('-gp', '--gcp_path', help="Path of the content packs in the GCP bucket",
                        required=False)
    parser.add_argument('-z', '--zip_path', help="Full path of folder to zip packs in", required=True)
    parser.add_argument('-b', '--bucket_name', help="Storage bucket name", required=True)
    parser.add_argument('-br', '--branch_name', help="Name of the branch", required=False)
    parser.add_argument('-n', '--circle_build', help="Number of the circle build", required=False)
    parser.add_argument('-s', '--service_account',
                        help=("Path to gcloud service account, is for circleCI usage. "
                              "For local development use your personal account and "
                              "authenticate using Google Cloud SDK by running: "
                              "`gcloud auth application-default login` and leave this parameter blank. "
                              "For more information go to: "
                              "https://googleapis.dev/python/google-api-core/latest/auth.html"),
                        required=False)
    parser.add_argument('-pvt', '--private', type=str2bool, help='Indicates if the tools is running '
                                                                 'on a private build.',
                        required=False, default=False)
    parser.add_argument('-rt', '--remove_test_playbooks', type=str2bool,
                        help='Whether to remove test playbooks from content packs or not.', default=True)

    return parser.parse_args()


def zip_packs(packs, destination_path):
    """
    Zips packs to a provided path.
    Args:
        packs: The packs to zip
        destination_path: The destination path to zip the packs in.
    """

    with ZipFile(os.path.join(destination_path, ARTIFACT_NAME), mode='w') as zf:
        for zip_pack in packs:
            for name, path in zip_pack.items():
                logging.info(f'Adding {name} to the zip file')
                zf.write(path, f'{name}.zip')


def remove_test_playbooks_if_exist(zips_path, packs):
    """
    If a pack has test playbooks, the function extracts the pack, removes the test playbooks and zips the pack again.
    Args:
        zips_path: The path where the pack zips are.
        packs: The packs name and path.
    """
    for zip_pack in packs:
        for name, path in zip_pack.items():
            remove = False
            with ZipFile(path, mode='r') as pack_zip:
                zip_contents = pack_zip.namelist()
                dir_names = [os.path.basename(os.path.dirname(content)) for content in zip_contents]
                if 'TestPlaybooks' in dir_names:
                    remove = True
                    logging.info(f'Removing TestPlaybooks from the pack {name}')
                    pack_path = os.path.join(zips_path, name)
                    print("packpath: " + pack_path)
                    pack_zip.extractall(path=pack_path,
                                        members=(member for member in zip_contents if 'TestPlaybooks' not in member))
                    remove_test_playbooks_from_signatures(pack_path, zip_contents)
            if remove:
                # Remove the current pack zip
                os.remove(path)
                shutil.make_archive(pack_path, 'zip', pack_path)


def remove_test_playbooks_from_signatures(path, filenames):
    """
    Remove the test playbook entries from the signatures file
    Args:
        path: The path of the pack
        filenames: The names of the files in the pack

    """
    signature_file_path = os.path.join(path, 'signatures.sf')
    test_playbooks = [file_ for file_ in filenames if 'TestPlaybooks' in file_]
    if os.path.isfile(signature_file_path):
        with open(signature_file_path, 'r') as signature_file:
            signature = json.load(signature_file)
            for test_playbook in test_playbooks:
                del signature[test_playbook]
        with open(signature_file_path, 'w') as signature_file:
            json.dump(signature, signature_file)
    else:
        logging.warning(f'Could not find signatures in the pack {os.path.basename(os.path.dirname(path))}')


def remove_unnecessary_files(zip_path):
    zip_path = zip_path + '/packs'
    dir_entries = os.listdir(zip_path)
    print(f"entires are: {', '.join(dir_entries)}")
    for entry in dir_entries:
        print(f"Current entry is: {entry}")
        entry_path = zip_path + os.sep + entry

        if entry in IGNORED_FILES:
            print(f"Found ignored file:{entry_path}, removing.")
            os.remove(entry_path)
            continue

        if os.path.isdir(entry_path):
            # This is a pack directory, should keep only most recent release zip
            print(f"current dir is: {entry_path}")
            pack_files = []
            for root, dirnames, filenames in os.walk(entry_path):
                # going over pack directory
                for filename in filenames:
                    pack_files.append(os.path.join(root, filename))
            print(f"files in {entry_path} are {', '.join(pack_files)}")
            latest_zip = get_latest_pack_zip_from_blob(entry, pack_files)
            print(f"Latest zip is {latest_zip}")
            for pack_file in pack_files:
                if pack_file != latest_zip:
                    print(f"Found unnecessary file:{pack_file}, removing.")
                    os.remove(pack_file)
        else:
            print(f"Found ignored file:{entry_path}, removing.")
            os.remove(entry_path)
            continue
    #
    #
    # for subdir, dirs, files in os.walk(zip_path):
    #     for filename in files:
    #         filepath = subdir + os.sep + filename
    #         print(f"Checking remove for {filename}")
    #         if filename in IGNORED_FILES:
    #             print(f"Found ignored file:{filepath}, removing.")
    #             os.remove(filepath)
    #
    #     for current_dir in dirs:
    #         print(f"current dir is: {current_dir}")
    #         if current_dir in IGNORED_FILES:
    #             print(f"Found ignored file:{current_dir}, removing.")
    #             os.remove(filepath)
    #         pack_files = []
    #         for root, dirnames, filenames in os.walk(current_dir):
    #             print("root is: " + root)
    #             pack_files.append(os.path.join(root, filenames))
    #         print(f"files in {current_dir} are {''.join(pack_files)}")
    #         latest_zip = get_latest_pack_zip_from_blob(current_dir, pack_files)
    #         for pack_file in pack_files:
    #             print(f"Found unnecessary file:{pack_file}, removing.")
    #             if pack_file == latest_zip:
    #                 os.remove(pack_file)


def get_zipped_packs_names(zip_path):
    zipped_packs = []
    for subdir, dirs, files in os.walk(zip_path):
        for filename in files:
            filepath = subdir + os.sep + filename
            print(f"Adding file of {filepath}")
            if not filename.endswith(".zip"):
                print(f"NOT A ZIP!!!!! - {filepath}")
            zipped_packs.append({Path(filepath).stem: filepath})
    if not zipped_packs:
        logging.critical('Did not find any pack to download from GCP.')
        sys.exit(1)
    return zipped_packs


def download_packs_from_gcp(storage_bucket, gcp_path, destination_path, circle_build, branch_name):
    """
    Iterates over the Packs directory in the content repository and downloads each pack (if found) from a GCP bucket
    in parallel.
    Args:
        storage_bucket: The GCP bucket to download from.
        gcp_path: The path of the packs in the GCP bucket.
        destination_path: The path to download the packs to.
        branch_name: The branch name of the build.
        circle_build: The number of the circle ci build.

    Returns:
        zipped_packs: A list of the downloaded packs paths and their corresponding pack names.
    """

    src_path = "gs://marketplace-dist-dev/" + gcp_path

    # CHANGE SRC PATH
    # if gcp_path == BUILD_GCP_PATH:
    #     src_path = os.path.join(gcp_path, branch_name, circle_build, 'content', 'packs')
    # else:
    #     src_path = os.path.join(gcp_path, branch_name, circle_build)
    #
    # if not branch_name or not circle_build:
    #     src_path = src_path.replace('/builds/content', '')

    try:
        process = subprocess.Popen(["Tests/scripts/cp_gcp_dir.sh", src_path, destination_path])
        out, err = process.communicate()
        if out:
            print("out" + out.decode('utf-8'))
        if err:
            print("err" + err.decode('utf-8'))
    except Exception as e:
        logging.critical(f"Failed to run cp_gcp_dir.sh, Error:{e}")
        sys.exit(1)

    # COPY TO DIFFERENT DIR IF NEEDED
    # should perform after manipulating the packs
    # if os.path.exists('/home/runner/work/content-private/content-private/content/artifacts/'):
    #     logging.info(f"Copying pack from {destination_path} to /home/runner/work/content-private/"
    #                  f"content-private/content/artifacts/packs")
    #     shutil.copy(destination_path,
    #                 f'/home/runner/work/content-private/content-private/content/artifacts/'
    #                 f'packs')


def executor_submit(executor, download_path, blob):
    executor.submit(blob.download_to_filename, download_path)


def cleanup(destination_path):
    """
    Cleans up the destination path directory by removing everything except the packs zip.
    Args:
        destination_path: The destination path.
    """
    files_to_remove = [file_.path for file_ in os.scandir(destination_path) if file_.name != ARTIFACT_NAME]
    for file_ in files_to_remove:
        if os.path.isdir(file_):
            shutil.rmtree(file_)
        else:
            os.remove(file_)


def get_latest_pack_zip_from_blob(pack, blobs):
    """
    Returns the latest zip of a pack from a list of blobs.
    Args:
        pack: The pack name
        blobs: The blob list

    Returns:
        blob: The zip blob of the pack with the latest version.
    """
    blob = None
    blobs = [b for b in blobs if os.path.splitext(os.path.basename(b))[0] == pack and b.endswith('.zip')]
    if blobs:
        blobs = sorted(blobs, key=lambda b: LooseVersion(os.path.basename(os.path.dirname(b))), reverse=True)
        blob = blobs[0]

    return blob


def main():
    install_logging('Zip_Content_Packs_From_GCS.log')
    option = option_handler()
    storage_bucket_name = option.bucket_name
    zip_path = option.zip_path
    artifacts_path = option.artifacts_path
    service_account = option.service_account
    circle_build = option.circle_build
    branch_name = option.branch_name
    gcp_path = 'content/packs'
    remove_test_playbooks = option.remove_test_playbooks
    private_build = option.private
    if private_build:
        packs_dir = '/home/runner/work/content-private/content-private/content/artifacts/packs'
        zip_path = '/home/runner/work/content-private/content-private/content/temp-dir'
        if not os.path.exists(packs_dir):
            logging.debug("Packs dir not found. Creating.")
            os.mkdir(packs_dir)
        if not os.path.exists(zip_path):
            logging.debug("Temp dir not found. Creating.")
            os.mkdir(zip_path)
        artifacts_path = '/home/runner/work/content-private/content-private/content/artifacts'

    # google cloud storage client initialized
    storage_client = init_storage_client(service_account)
    storage_bucket = storage_client.bucket('marketplace-dist-dev')

    if not circle_build or not branch_name:
        # Ignore build properties
        circle_build = ''
        branch_name = ''

    if not gcp_path:
        gcp_path = BUILD_GCP_PATH

    zipped_packs = []
    success = True
    try:
        # download file from storage_buckey+gcp_path to zip_path which is a temp
        # it will copy packs folder to zip_path
        download_packs_from_gcp(storage_bucket, gcp_path, zip_path, circle_build, branch_name)
    except Exception:
        logging.exception('Failed downloading packs')
        success = False

    # Keep only zip files of most recent releases
    remove_unnecessary_files(zip_path)

    # TODO: should copy to another dir what's left

    try:
        zipped_packs = get_zipped_packs_names(zip_path)
    except Exception:
        logging.exception('No zip files were found')
        success = False

    if zipped_packs and remove_test_playbooks:
        try:
            remove_test_playbooks_if_exist(zip_path, zipped_packs)
        except Exception:
            logging.exception('Failed removing test playbooks from packs')
            success = False

    if zipped_packs and success:
        try:
            zip_packs(zipped_packs, zip_path)
        except Exception:
            logging.exception('Failed zipping packs')
            success = False

        if success:
            logging.info('Successfully zipped packs.')
            if artifacts_path:
                # Save in the artifacts
                shutil.copy(os.path.join(zip_path, ARTIFACT_NAME), os.path.join(artifacts_path, ARTIFACT_NAME))
        else:
            logging.critical('Failed zipping packs.')
            sys.exit(1)
    else:
        logging.warning('Did not find any packs to zip.')

    cleanup(zip_path)


if __name__ == '__main__':
    main()
