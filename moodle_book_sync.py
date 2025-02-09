import os
import yaml
import requests
import json
from urllib.parse import urlparse, parse_qs
import logging
import argparse
import sys

class MoodleBookSync:
    def __init__(self, config_path='config.yaml', debug=False):
        with open(config_path, 'r') as f:
            self.config = yaml.safe_load(f)
        
        self.base_url = self.config['Moodle']['url']
        self.token = self.config['Moodle']['token']
        self.ws_url = f"{self.base_url}/webservice/rest/server.php"
        
        # Setup logging
        self.logger = logging.getLogger('MoodleBookSync')
        level = logging.DEBUG if debug else logging.INFO
        logging.basicConfig(
            level=level,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )

    def call_moodle_api(self, wsfunction, params=None):
        """Make a call to Moodle Web Services API"""
        data = {
            'wstoken': self.token,
            'moodlewsrestformat': 'json',
            'wsfunction': wsfunction,
            **(params or {})
        }
        
        self.logger.debug(f"Calling API endpoint: {wsfunction}")
        self.logger.debug(f"Request params: {params}")
        
        response = requests.post(self.ws_url, data=data)
        response.raise_for_status()
        
        response_data = response.json()
        self.logger.debug(f"Response: {json.dumps(response_data, indent=2)}")
        
        return response_data

    def extract_id_from_url(self, url):
        """Extract ID parameter from Moodle URL"""
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        return params.get('id', [None])[0]

    def get_course_module_info(self, cmid):
        """Get information about a course module"""
        return self.call_moodle_api('core_course_get_course_module', {
            'cmid': cmid
        })

    def get_book_chapters(self, book_id, course_url):
        """Get all chapters of a book"""
        course_id = self.extract_id_from_url(course_url)
        if not course_id:
            self.logger.error("Invalid course URL. Cannot extract course ID.")
            raise ValueError("Invalid course URL")
        
        # Use core_course_get_contents to fetch all course sections and modules
        course_contents = self.call_moodle_api('core_course_get_contents', {'courseid': course_id})
        
        book_json = next(
            (module for section in course_contents for module in section.get('modules', [])
             if module.get('modname') == 'book' and module.get('instance') == book_id),
            None
        )
        if book_json is None:
            self.logger.error("Book module with book_id %s not found.", book_id)
        else:
            # Process the book_json object further as needed
            # Locate the "structure" entry in the book_json contents which holds the chapters tree.
            structure_entry = next((item for item in book_json.get("contents", [])
                                     if item.get("filename") == "structure"), None)
            if not structure_entry:
                self.logger.error("Structure file not found in book module with book_id %s.", book_id)
                raise ValueError("Structure file not found in book module.")
            
            try:
                chapters_structure = json.loads(structure_entry.get("content", "[]"))
            except json.JSONDecodeError as e:
                self.logger.error("Failed to decode structure JSON: %s", e)
                raise ValueError("Invalid structure JSON in book module.") from e
            
            def flatten_chapters(chapters_list):
                result = []
                for chapter in chapters_list:
                    href = chapter.get("href", "")

                    chapter_dict = {
                        "url": href,
                        "metadata": {key: value for key, value in chapter.items() if key != "href"},
                        "is_subchapter": chapter.get("level", 0) > 0
                    }
                    result.append(chapter_dict)
                    if chapter.get("subitems"):
                        result.extend(flatten_chapters(chapter["subitems"]))
                return result
            
            chapters = flatten_chapters(chapters_structure)
            for chapter in chapters:
                matching_chapter_entry = next(
                    (entry for entry in book_json.get("contents", [])
                    if (entry.get("filename") == "index.html" and
                        chapter["url"].startswith(entry.get("filepath", "")[1:]))),
                    None
                )
                if matching_chapter_entry:
                    chapter["title"] = matching_chapter_entry.get("content", "")
                else:
                    self.logger.warning("Chapter with URL %s not found in book_json contents.", chapter["url"])
                    chapter["title"] = ""
                chapter["dirname"] = "/" + os.path.dirname(matching_chapter_entry.get("fileurl", "").split("pluginfile.php/")[-1]) + "/"
                chapter["fileurl"] = matching_chapter_entry.get("fileurl", "")
            return chapters

        self.logger.error(f"Book module with instance id {book_id} not found in course contents.")
        raise ValueError("Book module not found in the course contents.")

    def pull_book(self, book_config):
        """Download all chapters from a Moodle book"""
        cmid = self.extract_id_from_url(book_config['book_url'])
        if not cmid:
            raise ValueError("Invalid book URL")

        # Get book instance id
        module_info = self.get_course_module_info(cmid)
        book_id = module_info['cm']['instance']

        # Create directory if it doesn't exist
        directory = book_config['directory']
        os.makedirs(directory, exist_ok=True)

        # Get all chapters
        chapters = self.get_book_chapters(book_id, book_config['course_url'])
        
        for order, chapter in enumerate(chapters, 1):
            url_path = chapter["url"].replace("index.html", "").replace("/", "")
            chapter_filename = os.path.join(directory, f"{url_path}.html")
            meta_filename = os.path.join(directory, f"{url_path}.meta.json")
            
            if chapter.get("fileurl"):
                try:
                    response = requests.get(chapter["fileurl"] + "?token=" + self.token)
                    response.raise_for_status()
                    content = response.text
                except Exception as e:
                    self.logger.error("Failed to retrieve content for chapter %s: %s", chapter["url"], e)
                    content = ""
            else:
                self.logger.warning("No fileurl for chapter %s", chapter["url"])
                content = ""
            
            with open(chapter_filename, 'w', encoding='utf-8') as html_file:
                html_file.write(content)
            
            with open(meta_filename, 'w', encoding='utf-8') as meta_file:
                json.dump(chapter, meta_file, ensure_ascii=False, indent=2)

    def push_book(self, book_config, chapter_name=None):
        """Upload chapters back to Moodle
        
        Args:
            book_config: Dictionary containing book configuration
            chapter_name: Optional name of specific chapter to push (without .html extension)
        """
        directory = book_config['directory']
        if not os.path.exists(directory):
            raise ValueError(f"Directory {directory} does not exist")

        # Get all chapter files
        files = os.listdir(directory)
        chapter_files = sorted([f for f in files if f.endswith('.html')])

        if chapter_name:
            chapter_file = f"{chapter_name}.html"
            if chapter_file not in chapter_files:
                raise ValueError(f"Chapter file not found: {chapter_file}")
            chapter_files = [chapter_file]

        for chapter_file in chapter_files:
            base_filename = chapter_file[:-5]  # Remove .html
            metadata_file = f"{base_filename}.meta.json"
            
            if not os.path.exists(os.path.join(directory, metadata_file)):
                self.logger.warning(f"Skipping {chapter_file}: No metadata file found")
                continue
            
            # Read chapter content and metadata
            with open(os.path.join(directory, chapter_file), 'r', encoding='utf-8') as f:
                content = f.read()
            
            with open(os.path.join(directory, metadata_file), 'r', encoding='utf-8') as f:
                metadata = json.load(f)

            # Update chapter in Moodle
            try:
                self._update_chapter(content, metadata, chapter_name)
                self.logger.info(f"Successfully pushed chapter: {base_filename}")
            except Exception as e:
                self.logger.error(f"Failed to push chapter {base_filename}: {str(e)}")
                raise

    def _update_chapter(self, content, metadata, chapter_name):
        """Update a single chapter in Moodle"""

        # Not implemented yet
        raise NotImplementedError("Chapter update functionality not yet implemented")
        # # return self.call_moodle_api('mod_book_edit_chapter', {
        # #     'chapterid': chapter_name,
        # #     'content': content,
        # #     'title': metadata['title'],
        # #     'subchapter': 1 if metadata['is_subchapter'] else 0
        # # })
        # # Get chapter content from Moodle directly via HTTP request
        # # Upload file to Moodle's webservice/upload.php endpoint
        # upload_url = self.base_url + "/webservice/upload.php"
        # files = {'file_1': ('index.html', content.encode('utf-8'), 'text/html')}
        # params = {'token': self.token, "filepath": metadata['dirname']}
        
        # # Make upload request
        # upload_response = requests.post(upload_url, files=files, params=params)
        # print(upload_response.text)
        # if not upload_response.ok:
        #     raise Exception(f"Failed to upload file: {upload_response.text}")


def main():
    parser = argparse.ArgumentParser(description='Sync Moodle books with local files')
    parser.add_argument('command', choices=['pull', 'push'], help='Command to execute (pull or push)')
    parser.add_argument('--book', help='Name of the book to sync (if not specified, pulls all books)')
    parser.add_argument('--chapter', help='Specific chapter file to push (without .html extension)')
    parser.add_argument('--debug', action='store_true', help='Enable debug logging')
    parser.add_argument('--config', default='config.yaml', help='Path to config file')
    
    args = parser.parse_args()
    
    syncer = MoodleBookSync(config_path=args.config, debug=args.debug)
    
    if args.command == 'pull':
        if args.book:
            # Pull specific book
            book_config = next((book for book in syncer.config['Books'] 
                              if book.get('name') == args.book), None)
            if not book_config:
                print(f"Error: Book '{args.book}' not found in config", file=sys.stderr)
                sys.exit(1)
            syncer.pull_book(book_config)
        else:
            # Pull all books
            for book in syncer.config['Books']:
                syncer.pull_book(book)
    
    elif args.command == 'push':
        if not args.book:
            print("Error: Book name must be specified for push command", file=sys.stderr)
            sys.exit(1)
        
        book_config = next((book for book in syncer.config['Books'] 
                          if book.get('name') == args.book), None)
        if not book_config:
            print(f"Error: Book '{args.book}' not found in config", file=sys.stderr)
            sys.exit(1)
        
        try:
            syncer.push_book(book_config, args.chapter)
        except Exception as e:
            print(f"Error: {str(e)}", file=sys.stderr)
            sys.exit(1)

if __name__ == "__main__":
    main() 
