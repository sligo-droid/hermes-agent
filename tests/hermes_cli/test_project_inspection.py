from hermes_cli.config import DEFAULT_CONFIG
from hermes_cli.project_inspection import (
    MAX_PROJECT_INSPECTION_CANDIDATES,
    normalize_github_repo,
    normalize_project_inspection_candidates,
    normalize_project_inspection_url,
    resolve_project_inspection,
)


def _project(*, repo="sligo-labs/example", development=None, production=None):
    return {
        "github_repo": repo,
        "inspection": {
            "development_urls": development or [],
            "production_urls": production or [],
        },
    }


def test_core_projects_default_is_empty_and_generic():
    assert DEFAULT_CONFIG["projects"] == {}


def test_normalizes_exact_github_repo_forms():
    expected = "sligo-labs/example"
    assert normalize_github_repo("Sligo-Labs/Example.git") == expected
    assert normalize_github_repo("git@github.com:Sligo-Labs/Example.git") == expected
    assert normalize_github_repo("ssh://git@github.com/Sligo-Labs/Example.git") == expected
    assert normalize_github_repo("https://github.com/Sligo-Labs/Example/") == expected
    assert normalize_github_repo("https://gitlab.com/sligo-labs/example") is None
    assert normalize_github_repo("https://github.com/sligo-labs/example/issues") is None


def test_exact_repo_match_precedes_explicit_project_key():
    projects = {
        "wrong-explicit-key": _project(
            repo="sligo-labs/wrong",
            production=["https://wrong.example.test"],
        ),
        "repo-match": _project(
            repo="sligo-labs/right",
            production=["https://right.example.test"],
        ),
    }

    resolution = resolve_project_inspection(
        projects,
        github_repo="git@github.com:SLIGO-LABS/RIGHT.git",
        project_key="wrong-explicit-key",
    )

    assert resolution.project_key == "repo-match"
    assert resolution.matched_by == "github_repo"
    assert [candidate.url for candidate in resolution.candidates] == [
        "https://right.example.test/"
    ]


def test_approved_repository_field_matches_exact_github_repo():
    resolution = resolve_project_inspection(
        {
            "pid": {
                "repository": "sligo-labs/PID",
                "inspection": {
                    "development_urls": ["http://localhost:3000"],
                },
            }
        },
        github_repo="git@github.com:SLIGO-LABS/PID.git",
    )

    assert resolution.project_key == "pid"
    assert resolution.matched_by == "github_repo"
    assert resolution.candidates[0].url == "http://localhost:3000/"


def test_explicit_key_is_exact_fallback():
    projects = {"Example": _project(production=["https://example.test"])}

    matched = resolve_project_inspection(projects, project_key="Example")
    unmatched = resolve_project_inspection(projects, project_key="example")

    assert matched.matched_by == "project_key"
    assert unmatched.candidates == ()


def test_candidates_are_ordered_deduped_and_bounded():
    external = [f"https://dev-{index}.example.test" for index in range(20)]
    projects = {
        "example": _project(
            development=[
                "https://dev-0.example.test:443/",
                "http://127.0.0.1:3000",
                "http://10.0.0.9:8080",
                *external,
            ],
            production=[
                "https://dev-1.example.test",
                "https://example.test",
            ],
        )
    }

    candidates = resolve_project_inspection(
        projects, github_repo="https://github.com/sligo-labs/example"
    ).candidates

    assert len(candidates) == MAX_PROJECT_INSPECTION_CANDIDATES
    assert [(candidate.environment, candidate.location) for candidate in candidates[:2]] == [
        ("development", "local"),
        ("development", "local"),
    ]
    assert all(candidate.environment == "development" for candidate in candidates)
    assert len({candidate.url for candidate in candidates}) == len(candidates)


def test_candidate_order_is_local_dev_then_external_dev_then_production():
    resolution = resolve_project_inspection(
        {
            "example": _project(
                development=[
                    "https://dev.example.test",
                    "http://localhost:3000",
                ],
                production=["https://example.test"],
            )
        },
        project_key="example",
    )

    assert [candidate.to_dict() for candidate in resolution.candidates] == [
        {
            "url": "http://localhost:3000/",
            "environment": "development",
            "location": "local",
        },
        {
            "url": "https://dev.example.test/",
            "environment": "development",
            "location": "external",
        },
        {
            "url": "https://example.test/",
            "environment": "production",
            "location": "external",
        },
    ]


def test_url_validation_rejects_unsafe_or_non_absolute_values():
    rejected = [
        "/relative",
        "file:///tmp/page.html",
        "https://user:password@example.test",
        "https://example.test/?access_token=secret",
        "https://example.test/?accessToken=secret",
        "https://example.test/?apiKey=secret",
        "https://example.test/?x-amz-signature=secret",
        "https://example.test/\nnext",
        "https://example.test/%0anext",
        "https://example.test/" + "x" * 2048,
    ]

    for value in rejected:
        assert normalize_project_inspection_url(value, environment="development") is None

    assert (
        normalize_project_inspection_url(
            "https://example.test/page?theme=dark", environment="development"
        )
        == "https://example.test/page?theme=dark"
    )


def test_private_targets_are_development_only():
    for value in (
        "http://localhost:3000",
        "http://192.168.1.10:8080",
        "http://service.internal",
    ):
        assert normalize_project_inspection_url(value, environment="development")
        assert normalize_project_inspection_url(value, environment="production") is None


def test_serialized_candidates_are_revalidated_and_reordered():
    candidates = normalize_project_inspection_candidates(
        [
            {
                "url": "https://production.example.test",
                "environment": "production",
                "location": "local",
            },
            {
                "url": "http://localhost:3000",
                "environment": "development",
                "location": "external",
            },
            {
                "url": "https://example.test/?token=secret",
                "environment": "development",
                "location": "external",
            },
        ]
    )

    assert [candidate.to_dict() for candidate in candidates] == [
        {
            "url": "http://localhost:3000/",
            "environment": "development",
            "location": "local",
        },
        {
            "url": "https://production.example.test/",
            "environment": "production",
            "location": "external",
        },
    ]
