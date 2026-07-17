import subprocess
import sys
import os

def main():
    print("Starting build process for SerenityDev extension...")
    
    # Ensure we are in the right directory
    workspace_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(workspace_dir)

    try:
        # Run npx vsce package to build the .vsix
        # Using npx allows us to run vsce without requiring it to be globally installed
        print("Running 'npx @vscode/vsce package'...")
        
        # Shell=True is often required on Windows to run npx
        result = subprocess.run(
            ["npx", "@vscode/vsce", "package", "--no-dependencies"], 
            cwd=workspace_dir, 
            shell=True,
            check=True,
            text=True
        )
        
        print("\nSuccess! The .vsix file has been updated/created.")
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
