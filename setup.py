from setuptools import setup, find_packages

setup(
    name="RAG_Document_Retrieval",
    version="1.0.0",
    packages=find_packages(),
    include_package_data=True,
    license="MIT",
    install_requires=[
        "django>=4.2",
    ],
)