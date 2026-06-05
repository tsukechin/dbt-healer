import subprocess
import time
from pathlib import Path

import questionary
import typer
from rich.console import Console
from rich.markdown import Markdown

from common.config import get_config
from common.exceptions import DBTProfilesExistsError
from app.ci_generator import GithubCIGenerator, GitlabCIGenerator
from app.provider_builder import OllamaProviderType, ProviderType, build_provider

console = Console()
config = get_config()

app = typer.Typer(help="DBT Healer CLI")


def _yes_no(value: bool) -> str:
    """Return setup choice label for a boolean value."""
    return "Yes" if value else "No"


def _ask_enabled(question: str, current: bool = True) -> bool:
    """Ask a yes/no setup question and return boolean answer."""
    answer = questionary.select(
        question,
        choices=["Yes", "No"],
        default=_yes_no(current),
    ).ask()
    return answer == "Yes"


@app.command(help="Setup your enviroment for dbt-healer")
def setup():
    """Run interactive dbt-healer setup."""
    config_dict = {}
    first_setup = True
    answer = ""

    if Path(".env").exists():
        first_setup = False

        console.print("[yellow]Warning: .env file already exists.[/yellow]")
        time.sleep(2)
        answer = questionary.select(
            "Proceed with full setup or select the current step? (Full setup will fully replace the .env file)",
            choices=["Full setup", "Current step", "Exit from setup"],
        ).ask()
        
        if answer == "Exit from setup":
            return

    change_parameter = None
    if answer == 'Current step':
        change_parameter = questionary.select(
            "Which parameter do you want to overwrite?",
            choices=[
                "Analyze url",
                "Repository path",
                "Name of dbt project",
                "Telegram Bot Token",
                "AI Provider and Token",
                "Git platform and git parameters",
                "CI behavior",
            ],
        ).ask()
    else:
        first_setup = True

    if change_parameter == 'Analyze url' or first_setup:
        console.print(Markdown("1. Write the URL where errors from failed CI pipelines will be received (default is `localhost`):"))
        config_dict["service_endpoint"] = input(">>> ")

    if change_parameter == 'Name of dbt project' or first_setup:
        console.print(Markdown("2. What is the name of your dbt project? (e.g., `my_dbt_project`)"))
        config_dict["dbt_project_name"] = input(">>> ")

    if not change_parameter:
        answer = questionary.select(
            "Configure Telegram notifications?",
            choices=["Yes", "No"],
        ).ask()

    if (answer == "Yes" and first_setup) or change_parameter == 'Telegram Bot Token':
        console.print(Markdown("3. Telegram Bot Token: (needed for notifications, optional)"))
        config_dict["telegram_bot_token"] = input(">>> ")

    if change_parameter == 'AI Provider and Token' or first_setup:
        console.print(Markdown("4. What provider do you want to use for AI analysis?"))
        answer = questionary.select(
            "Select AI provider:",
            choices=[provider.value for provider in ProviderType],
        ).ask()

        ai_provider_type = None
        if answer == "Ollama":
            console.print(Markdown("5. Select Ollama connection type:"))
            ai_provider_type = questionary.select(
                "Select AI provider:",
                choices=[provider_type.value for provider_type in OllamaProviderType],
            ).ask()

        if ai_provider_type == OllamaProviderType.LOCAL.value:
            config_dict["ai_api_key"] = ""
        else:
            console.print(Markdown("6. Enter your API key for the selected AI provider:"))
            config_dict["ai_api_key"] = input(">>> ")

        config_dict["ai_provider"] = answer
        config_dict["ai_provider_type"] = ai_provider_type
        config.save({"ai_api_key": config_dict["ai_api_key"]})

        provider = build_provider(
            ai_provider=ProviderType(answer),
            ollama_type=ai_provider_type,
        )

        console.print(Markdown("7. Which model do you prefer to use?"))
        answer = questionary.select(
            "Select model:",
            choices=provider.get_models_list(),
        ).ask()
        config_dict["ai_model"] = answer

    if change_parameter == 'Git platform and git parameters' or first_setup:
        console.print(Markdown("8. Which git platform do you use?"))
        answer = questionary.select(
            "Select platform:",
            choices=["Github", "GitLab"],
        ).ask()
        config_dict["git_platform"] = answer

        console.print(Markdown(f"9. Enter the {answer} repository link:"))
        config_dict["github_repo_link"] = input(">>> ")

        console.print(Markdown(f"10. Enter the base branch of your {answer} repository (e.g., `master` or `main`):"))
        config_dict["base_branch"] = input(">>> ")

        console.print(Markdown(f"11. Enter your {answer} token (needed for creating pull requests):"))
        config_dict["github_token"] = input(">>> ")
        config.save({"github_token": config_dict["github_token"]})

        console.print(Markdown(f"12. Enter your full local path of your {answer} repository:"))
        config_dict["full_path_to_repo_str"] = input(">>> ")

    if change_parameter == 'CI behavior' or first_setup:
        console.print(Markdown("13. Configure dbt-healer CI behavior:"))
        config_dict["healer_review_enabled"] = _ask_enabled(
            "Run review before dbt build?",
            config.healer_review_enabled,
        )
        config_dict["healer_analyze_on_failure_enabled"] = _ask_enabled(
            "Analyze failed dbt builds and create PR/MR?",
            config.healer_analyze_on_failure_enabled,
        )

    console.print(Markdown("Saving configuration..."))
    time.sleep(1)

    if config.save(config_dict):
        console.print(
            "[green]Configuration saved successfully![/green]"
            "\nIf you want to change any value, you can edit the .env file in the project root directory."
        )
        time.sleep(1)

    if change_parameter in {'Git platform and git parameters', 'CI behavior'} or first_setup:
        platform = config_dict.get("git_platform") or config.git_platform
        generator = {
            "Github": GithubCIGenerator,
            "GitLab": GitlabCIGenerator,
        }[platform]()

        try:
            with console.status("Creating CI workflow..."):
                ci_file_status = generator.create_ci_file(force=change_parameter == 'CI behavior')

            ci_profile_status = "skipped"
            if change_parameter != 'CI behavior':
                with console.status("Creating CI profile..."):
                    ci_profile_status = generator.create_ci_profile()

        except DBTProfilesExistsError as exc:
            console.print(f"[red]{exc}[/red]")
            return
        except Exception as exc:
            console.print(f"[red]Failed to configure CI: {exc}[/red]")
            return

        if ci_file_status == "exists":
            console.print("[yellow]CI workflow already exists. Skipping creation...[/yellow]")
        if ci_profile_status == "exists":
            console.print("[yellow]CI profile already exists. Skipping creation...[/yellow]")

        console.print("[green]CI configured successfully![/green]")


@app.command(help="Serve dbt-healer analyzer")
def serve(
    port: int = 8888,
    logs: bool = typer.Option(False, "--logs", help="Stream Docker Compose logs after startup."),
):
    """Start analyzer services with Docker Compose."""
    console.print(Markdown(f"Starting **dbt-healer** server on port {port}..."))
    config = get_config()

    config.save({"service_port": str(port)}) 
    subprocess.run(["docker", "compose", "up", "-d"], check=True)

    console.print("[green]Server is running![/green]")
    if logs:
        console.print("[cyan]Streaming logs. Press Ctrl+C to stop.[/cyan]")
        try:
            subprocess.run(["docker", "compose", "logs", "-f"], check=False)
        except KeyboardInterrupt:
            console.print("\n[yellow]Log streaming stopped.[/yellow]")


if __name__ == "__main__":
    app()
