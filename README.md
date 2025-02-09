# Moodle Book Sync

Moodle Book Sync is a Python script designed to synchronize Moodle books with local files. It allows you to pull book chapters from Moodle and save them locally, as well as push local changes back to Moodle (not implemented yet).

## Features

- Pull book chapters from Moodle and save them as HTML files.
- Push local HTML files back to Moodle (not implemented yet).
- Supports multiple books and chapters.
- Configurable via a YAML configuration file.

## Requirements

- Python 3.6+
- `requests` library
- `PyYAML` library

## Installation

1. Clone the repository:
    ```sh
    git clone https://github.com/rrader/moodle-git-book.git
    cd moodle-book-sync
    ```

2. Install the required Python packages:
    ```sh
    pip install -r requirements.txt
    ```

## Configuration

Create a `config.yaml` file in the root directory with the following structure:

```yaml
# Moodle configuration
Moodle:
  # Base URL of your Moodle instance (without trailing slash)
  url: "https://moodle.example.com"
  # Your Moodle Web Services token
  token: "your_moodle_token_here"

# Books to sync
Books:
  # Example of a book configuration
  - book_url: "https://moodle.example.com/mod/book/view.php?id=12345"
    course_url: "https://moodle.example.com/course/view.php?id=67890"
    name: "engineering"
    directory: "engineering"  # Local directory to store book contents

  # You can add multiple books
  - book_url: "https://moodle.example.com/mod/book/view.php?id=98765"
    course_url: "https://moodle.example.com/course/view.php?id=43210"
    name: "mathematics"
    directory: "mathematics" 
```

## Usage

### Pulling Book Chapters

To pull book chapters from your Moodle instance, run the following command:

```sh
python moodle_book_sync.py pull --book <book_name>
```

Replace `<book_name>` with the name of the book you want to pull.

