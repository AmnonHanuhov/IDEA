/**
 * @file file_utils.c
 * @author Asaf Levi
 * CREATED FOR IDEA
 */
#include "file_utils.h"

int count_lines_of_file(FILE* f)
{
	int lines = 0;
	int ch, last = '\n'; // an empty file has no lines
	fseek(f, 0, SEEK_SET);

	while ((ch = fgetc(f)) != EOF)
	{
		if (ch == '\n')
		{
			lines++;
		}
		last = ch;
	}
	if (last != '\n')
	{
		lines++; // the last line has no \n after it
	}

	return lines;
}
