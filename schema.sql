-- OpenShelf database schema
-- Run against the database selected by DB_NAME. CREATE TABLE IF NOT EXISTS
-- is safe to run on an existing database.

CREATE TABLE IF NOT EXISTS users (
    id INT AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(50) NOT NULL,
    email VARCHAR(120) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    role ENUM('user', 'admin') NOT NULL DEFAULT 'user',
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    display_name VARCHAR(80) NULL,
    bio VARCHAR(500) NULL,
    favourite_genres VARCHAR(255) NULL,
    favourite_book VARCHAR(120) NULL,
    currently_reading VARCHAR(120) NULL,
    annual_reading_goal SMALLINT UNSIGNED NULL,
    profile_filename VARCHAR(255) NULL,
    guidelines_accepted_at TIMESTAMP NULL DEFAULT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY unique_username (username),
    UNIQUE KEY unique_email (email)
);

CREATE TABLE IF NOT EXISTS books (
    id INT AUTO_INCREMENT PRIMARY KEY,
    title VARCHAR(200) NOT NULL,
    author VARCHAR(200) NOT NULL,
    genre VARCHAR(80) NOT NULL,
    cover_url VARCHAR(500) NULL,
    cover_filename VARCHAR(255) NULL,
    description TEXT NULL,
    isbn VARCHAR(20) NULL,
    publisher VARCHAR(200) NULL,
    publication_year SMALLINT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_books_title (title),
    INDEX idx_books_author (author),
    INDEX idx_books_genre (genre)
);

CREATE TABLE IF NOT EXISTS reviews (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    book_id INT NOT NULL,
    rating DECIMAL(2, 1) NOT NULL,
    review_text TEXT NOT NULL,
    is_spoiler TINYINT(1) NOT NULL DEFAULT 0,
    status ENUM('pending', 'approved', 'rejected') NOT NULL DEFAULT 'pending',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY unique_user_book_review (user_id, book_id),
    INDEX idx_reviews_book_status (book_id, status),
    CONSTRAINT fk_reviews_user
        FOREIGN KEY (user_id) REFERENCES users(id)
        ON DELETE CASCADE,
    CONSTRAINT fk_reviews_book
        FOREIGN KEY (book_id) REFERENCES books(id)
        ON DELETE CASCADE,
    CONSTRAINT chk_review_rating_range
        CHECK (rating >= 1.0 AND rating <= 5.0),
    CONSTRAINT chk_review_rating_step
        CHECK (rating * 2 = FLOOR(rating * 2))
);

CREATE TABLE IF NOT EXISTS reading_list (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    book_id INT NOT NULL,
    status ENUM('want_to_read', 'currently_reading', 'finished')
        NOT NULL DEFAULT 'want_to_read',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY unique_user_book_list (user_id, book_id),
    CONSTRAINT fk_reading_list_user
        FOREIGN KEY (user_id) REFERENCES users(id)
        ON DELETE CASCADE,
    CONSTRAINT fk_reading_list_book
        FOREIGN KEY (book_id) REFERENCES books(id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS review_votes (
    id INT AUTO_INCREMENT PRIMARY KEY,
    review_id INT NOT NULL,
    user_id INT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY unique_user_review_vote (review_id, user_id),
    CONSTRAINT fk_votes_review
        FOREIGN KEY (review_id) REFERENCES reviews(id)
        ON DELETE CASCADE,
    CONSTRAINT fk_votes_user
        FOREIGN KEY (user_id) REFERENCES users(id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS review_comments (
    id INT AUTO_INCREMENT PRIMARY KEY,
    review_id INT NOT NULL,
    user_id INT NOT NULL,
    comment_text VARCHAR(1000) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_comments_review
        FOREIGN KEY (review_id) REFERENCES reviews(id)
        ON DELETE CASCADE,
    CONSTRAINT fk_comments_user
        FOREIGN KEY (user_id) REFERENCES users(id)
        ON DELETE CASCADE
);
