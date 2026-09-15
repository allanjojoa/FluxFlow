from setuptools import setup, find_packages

setup(
    name="fluxflow",
    version="0.1.0",
    description="Modular release management for Talend, Snowflake, and Tableau",
    packages=find_packages(),
    python_requires=">=3.10",
    install_requires=[
        "requests>=2.31.0",
        "pyyaml>=6.0",
        "rich>=13.0.0",
    ],
    entry_points={
        "console_scripts": [
            "fluxflow=fluxflow.cli:main",
        ],
    },
)
