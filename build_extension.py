import subprocess
import sys
import os
import json
import shutil

def main():
    print("Starting build process for SerenityDev extension...")
    workspace_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(workspace_dir)

    try:
        print("Compiling extension entrypoint via 'npm run package'...")
        npm_command = shutil.which("npm") or shutil.which("npm.cmd")
        if not npm_command:
            print("'npm' was not found on PATH. Install Node.js and npm first.")
            sys.exit(1)
        subprocess.run([npm_command, "run", "package"], cwd=workspace_dir, check=True)

        local_vsce = os.path.join(workspace_dir, "node_modules", ".bin", "vsce.cmd" if os.name == "nt" else "vsce")
        vsce_command = local_vsce if os.path.isfile(local_vsce) else (shutil.which("vsce") or shutil.which("vsce.cmd"))
        if not vsce_command:
            print("'vsce' was not found. Install project dependencies with 'npm install'.")
            sys.exit(1)

        print("Running 'vsce package --no-dependencies'...")
        shell = os.path.exists(vsce_command) and vsce_command.endswith(".cmd")
        _ = subprocess.run(
        [vsce_command, "package", "--no-dependencies"],
        cwd=workspace_dir,
        shell=shell,
        check=True,
        text=True
        )       
        vsix_path = None
        pkg_path = os.path.join(workspace_dir, "package.json")
        if os.path.exists(pkg_path):
            with open(pkg_path, "r", encoding="utf-8") as f:
                pkg = json.load(f)
            vsix_name = f"{pkg.get('name')}-{pkg.get('version')}.vsix"
            vsix_path = os.path.abspath(os.path.join(workspace_dir, vsix_name))

        if not vsix_path or not os.path.isfile(vsix_path):
            print("\nError: Packaging completed but the expected .vsix file was not created.")
            sys.exit(1)

        print("\nSuccess! The .vsix file has been updated/created.")
        if vsix_path:
            print(f"VSIX Path: {vsix_path}")
        print("You can now install it in VS Code by right-clicking the .vsix file and selecting 'Install Extension VSIX'.")
        
    except subprocess.CalledProcessError as e:
        print(f"\nError: Failed to package the extension. Command exited with code {e.returncode}.")
        print("Please ensure you have Node.js and npm installed.")
        sys.exit(1)
    except FileNotFoundError:
        print("\nError: 'npx' command not found. Please make sure Node.js is installed and in your system PATH.")
        sys.exit(1)

if __name__ == "__main__":
    main()

