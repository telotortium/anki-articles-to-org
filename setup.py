from setuptools import setup, find_packages

setup(
    name="anki-articles-to-org",
    version="0.1.0",
    author="Robert Irelan",
    author_email="rirelan@gmail.com",
    description="Exports Anki Article notes to Org-mode files",
    packages=find_packages(),
    install_requires=[
        "requests",
    ],
    entry_points={
        "console_scripts": [
            "anki-articles-to-org=anki_articles_to_org.__init__:main",
        ],
    },
)
