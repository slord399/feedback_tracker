import csv
import os

class Localizer:
    def __init__(self, csv_path):
        self.csv_path = csv_path
        self.translations = {}
        self.languages = []
        self.load()

    def load(self):
        if not os.path.exists(self.csv_path):
            return

        with open(self.csv_path, mode='r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            self.languages = [col for col in reader.fieldnames if col != 'string_name']

            for row in reader:
                name = row['string_name']
                self.translations[name] = row

    def get(self, string_name, lang="English", **kwargs):
        row = self.translations.get(string_name)
        if not row:
            return string_name

        text = row.get(lang)
        if not text or text.strip() == "":
            # Fallback to English
            text = row.get("English", string_name)

        try:
            return text.format(**kwargs)
        except KeyError:
            return text

localizer = None

def get_localizer():
    global localizer
    if localizer is None:
        # Assuming we are in Bot/shared, Locale is at ../../Locale
        path = os.path.join(os.path.dirname(__file__), "..", "..", "Locale", "template.csv")
        localizer = Localizer(path)
    return localizer
