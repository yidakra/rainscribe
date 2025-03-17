#!/bin/bash

# Prepare RainScribe for GitHub
echo "Preparing RainScribe for GitHub..."

# Check if git is installed
if ! command -v git &> /dev/null; then
    echo "Error: git is not installed. Please install git first."
    exit 1
fi

# Initialize git repository if not already initialized
if [ ! -d ".git" ]; then
    echo "Initializing git repository..."
    git init
fi

# Clean up any temporary files and directories
echo "Cleaning up temporary files..."
rm -rf rainscribe_output
rm -f rainscribe_run.log
rm -f *.ts
rm -f *.m3u8
rm -f index.html

# Make sure requirements.txt is up to date
echo "Checking requirements.txt..."
if [ ! -f "requirements.txt" ]; then
    echo "Error: requirements.txt not found."
    exit 1
fi

# Make sure LICENSE file is present
echo "Checking LICENSE file..."
if [ ! -f "LICENSE" ]; then
    echo "Warning: LICENSE file not found. Make sure you include a GNU GPL v3 license."
fi

# Make sure .env is not tracked
echo "Creating .env example file..."
if [ -f ".env" ]; then
    cp .env .env.example
    sed -i.bak 's/GLADIA_API_KEY=.*/GLADIA_API_KEY=your_api_key_here/' .env.example
    rm -f .env.example.bak
fi

# Add files to git
echo "Adding files to git..."
git add .
git status

echo "
Repository is ready for GitHub!

Next steps:
1. Review the changes with 'git status'
2. Commit the changes with: git commit -m 'Initial commit of RainScribe'
3. Create a repository on GitHub
4. Add the remote with: git remote add origin https://github.com/yourusername/rainscribe.git
5. Push the changes with: git push -u origin main

Note: Make sure to replace 'yourusername' with your actual GitHub username.
"

chmod +x prepare_for_github.sh 