from hhru_bot.skill_gaps import aggregate_skill_gaps


def test_aggregate_counts_each_vacancy_once_and_ranks():
    rows = aggregate_skill_gaps(
        ["Python Python, Docker", "Python and SQL", "Docker"], current_skills=[]
    )
    assert rows == [
        {"skill": "python", "vacancies": 2},
        {"skill": "docker", "vacancies": 2},
        {"skill": "sql", "vacancies": 1},
    ]


def test_aggregate_omits_resume_skills():
    assert aggregate_skill_gaps(["Python and Docker"], ["Python"]) == [
        {"skill": "docker", "vacancies": 1}
    ]
