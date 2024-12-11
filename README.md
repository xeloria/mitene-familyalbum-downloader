
# Mitene & Family-Album Downloader 5

Download media from [Mitene](https://mitene.us/) & [Family Album](https://family-album.com/). This script allows you to download photos, videos, and comments from the specified album URL and keep them stored locally on your machine.


## Demo
![gif](https://github.com/suasive93/mitene-familyalbum-downloader/assets/20932109/9c313b0f-6b36-4e35-9c81-b612e07aa2e7)

## Requirements
- Python 3.11 and up.

## Installation

- Install python.
- Clone the git repo or download the zip.
- pip install -r requirements.txt in command prompt or terminal.
    
## Usage

From mitene app, invite a family member for the web version and copy the URL 
( that should be something like https://mitene.us/f/abcd123456 )
- python mitene_download.py
- python mitene_download.py <INSERT_URL>
- python mitene_download.py <INSERT_URL> --password <INSERT_PASSWORD>



## Features

- Saving comments
- Cross platform
- Organizes media accordignly.


## Changelog

- Added and increased a timeout option.
- Changed the layout of the code to be user friendly and interactive. 
- Optimized the code for cleaner operation, easier to read.
- Removed the .tmp file writing, and made it to write files directly. 
- Added the ability to write video media files extension (.mp4) previously not able in version 1.
- Better network handling if lost connection.
- Proper exiting the script without causing errors.
- Added the ability to pre-check local files before downloading the same files.
- Updated user menu.  


## Authors

- [@perrinjerome](https://github.com/perrinjerome) Jérome Perrin (main developer)
- [@xeloria](https://github.com/xeloria/) Me (improved existing code)


